#include "AnisetteV3.h"

#include <chrono>
#include <ctime>
#include <cstdlib>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include <cpprest/http_client.h>
#include <cpprest/json.h>
#include <cpprest/ws_client.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <plist/plist.h>

#include "AnisetteData.h"
#include "ServerError.hpp"

#define odslog(msg) { std::stringstream ss; ss << msg << std::endl; OutputDebugStringA(ss.str().c_str()); }

using namespace web;
using namespace web::http;
using namespace web::http::client;

// The identity's keychain identifier and adi.pb are as good as a password for this device, and the
// one-time password and machine ID are anisette secrets: none of them goes to the log. The device ID
// is shown by its first 8 characters only.
namespace
{
	std::mutex stateMutex;

	struct ClientInfo
	{
		std::string clientInfo;
		std::string userAgent;
	};

	struct State
	{
		std::vector<unsigned char> identifier; // 16 random bytes
		std::vector<unsigned char> adiPB;      // empty until provisioned
	};

	struct Headers
	{
		std::string machineID;       // X-Apple-I-MD-M
		std::string oneTimePassword; // X-Apple-I-MD
		std::string routingInfo;     // X-Apple-I-MD-RINFO
	};

	ServerError V3Error(std::string message)
	{
		odslog("Anisette v3: " << message);
		return ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey, "Anisette v3: " + message }
		});
	}

	std::string TrimmedURL(std::string url)
	{
		while (!url.empty() && url.back() == '/')
		{
			url.pop_back();
		}

		return url;
	}

	// The proxy the environment names for url, for the anisette server and the provisioning requests:
	// HTTPS_PROXY for https and wss, HTTP_PROXY for http and ws, unless NO_PROXY lists the host ("*", the
	// host itself, or a domain it is in).
	std::optional<web_proxy> EnvironmentProxy(const std::string& url)
	{
		uri target(url);
		std::string host = target.host();

		for (const char* name : { "NO_PROXY", "no_proxy" })
		{
			const char* value = getenv(name);
			if (value == nullptr)
			{
				continue;
			}

			std::stringstream entries(value);
			std::string entry;
			while (std::getline(entries, entry, ','))
			{
				entry.erase(0, entry.find_first_not_of(" \t"));
				entry.erase(entry.find_last_not_of(" \t") + 1);
				if (!entry.empty() && entry[0] == '.')
				{
					entry.erase(0, 1);
				}

				bool isInDomain = host.size() > entry.size() && host.compare(host.size() - entry.size() - 1, std::string::npos, "." + entry) == 0;
				if (entry == "*" || (!entry.empty() && (host == entry || isInDomain)))
				{
					return std::nullopt;
				}
			}
		}

		bool isSecure = target.scheme() == "https" || target.scheme() == "wss";
		for (const char* name : isSecure ? std::vector<const char*>{ "HTTPS_PROXY", "https_proxy" } : std::vector<const char*>{ "HTTP_PROXY", "http_proxy" })
		{
			const char* value = getenv(name);
			if (value != nullptr && strlen(value) > 0)
			{
				return web_proxy(uri(value));
			}
		}

		return std::nullopt;
	}

	http_client MakeClient(std::string url, bool validateCertificates)
	{
		http_client_config config;
		config.set_validate_certificates(validateCertificates);
		config.set_timeout(std::chrono::seconds(60));
		if (auto proxy = EnvironmentProxy(url))
		{
			config.set_proxy(*proxy);
		}

		return http_client(url, config);
	}

	std::string Hex(const unsigned char* bytes, size_t length)
	{
		std::ostringstream stream;
		for (size_t i = 0; i < length; i++)
		{
			stream << std::hex << std::setw(2) << std::setfill('0') << (int)bytes[i];
		}

		return stream.str();
	}

	// X-Apple-I-MD-LU: SHA-256 of the identifier in hex, as omnisette sends it.
	std::string LocalUserID(const State& state)
	{
		unsigned char digest[SHA256_DIGEST_LENGTH];
		SHA256(state.identifier.data(), state.identifier.size(), digest);
		return Hex(digest, sizeof(digest));
	}

	// X-Mme-Device-Id: the identifier as an upper-case UUID.
	std::string DeviceID(const State& state)
	{
		std::string hex = Hex(state.identifier.data(), state.identifier.size());
		for (auto& c : hex)
		{
			c = toupper((unsigned char)c);
		}

		return hex.substr(0, 8) + "-" + hex.substr(8, 4) + "-" + hex.substr(12, 4) + "-" + hex.substr(16, 4) + "-" + hex.substr(20, 12);
	}

	std::string UTCTime(time_t time)
	{
		char buffer[32];
		struct tm tm;
		gmtime_r(&time, &tm);
		strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &tm);
		return buffer;
	}

	ClientInfo FetchClientInfo(const std::string& serverURL)
	{
		static std::mutex mutex;
		static std::map<std::string, ClientInfo> clientInfoByURL;

		std::lock_guard<std::mutex> lock(mutex);
		if (clientInfoByURL.count(serverURL) > 0)
		{
			return clientInfoByURL[serverURL];
		}

		auto client = MakeClient(serverURL, true);
		auto response = client.request(methods::GET, "/v3/client_info").get();
		if (response.status_code() != status_codes::OK)
		{
			throw V3Error(serverURL + "/v3/client_info answered HTTP " + std::to_string(response.status_code()) + ".");
		}

		auto json = response.extract_json(true).get();
		if (!json.has_field("client_info") || !json.at("client_info").is_string() || !json.has_field("user_agent") || !json.at("user_agent").is_string())
		{
			throw V3Error(serverURL + "/v3/client_info has no client_info and user_agent.");
		}

		clientInfoByURL[serverURL] = { json.at("client_info").as_string(), json.at("user_agent").as_string() };
		return clientInfoByURL[serverURL];
	}

	// <config>/AltServer/anisette-v3/<host>.json, where <config> is $XDG_CONFIG_HOME or ~/.config. A port or
	// path in the server URL is added to the name, so each server keeps its own identity.
	std::string StateDirectory()
	{
		const char* xdgConfigHome = getenv("XDG_CONFIG_HOME");
		const char* home = getenv("HOME");

		std::string base;
		if (xdgConfigHome != nullptr && strlen(xdgConfigHome) > 0)
		{
			base = xdgConfigHome;
		}
		else if (home != nullptr && strlen(home) > 0)
		{
			base = std::string(home) + "/.config";
		}
		else
		{
			throw V3Error("Neither XDG_CONFIG_HOME nor HOME is set, so there is nowhere to keep the anisette identity.");
		}

		return base + "/AltServer/anisette-v3";
	}

	std::string StatePath(const std::string& serverURL)
	{
		uri serverURI(serverURL);
		std::string name = serverURI.host();
		if (!serverURI.is_port_default())
		{
			name += "_" + std::to_string(serverURI.port());
		}

		std::string path = serverURI.path();
		if (path != "" && path != "/")
		{
			for (auto c : path)
			{
				name += isalnum((unsigned char)c) || c == '-' || c == '.' ? c : '_';
			}
		}

		return StateDirectory() + "/" + name + ".json";
	}

	void MakeDirectories(const std::string& path)
	{
		for (size_t position = 1; position <= path.size(); position++)
		{
			if (position == path.size() || path[position] == '/')
			{
				std::string directory = path.substr(0, position);
				if (mkdir(directory.c_str(), 0700) != 0 && errno != EEXIST)
				{
					throw V3Error("Couldn't create " + directory + ": " + strerror(errno));
				}
			}
		}
	}

	std::optional<State> LoadState(const std::string& path)
	{
		std::ifstream file(path);
		if (!file)
		{
			return std::nullopt;
		}

		try
		{
			std::stringstream contents;
			contents << file.rdbuf();
			auto json = json::value::parse(contents.str());

			State state;
			state.identifier = utility::conversions::from_base64(json.at("identifier").as_string());
			if (json.has_field("adi_pb") && json.at("adi_pb").is_string())
			{
				state.adiPB = utility::conversions::from_base64(json.at("adi_pb").as_string());
			}

			if (state.identifier.size() != 16)
			{
				throw std::runtime_error("identifier isn't 16 bytes");
			}

			return state;
		}
		catch (std::exception& exception)
		{
			odslog("Anisette v3: ignoring unreadable " << path << " (" << exception.what() << "), provisioning a new identity.");
			return std::nullopt;
		}
	}

	// Writes the state readable by its owner only (mode 600), replacing the old file in one step.
	void SaveState(const std::string& path, const State& state)
	{
		MakeDirectories(path.substr(0, path.find_last_of('/')));

		auto json = json::value::object();
		json["identifier"] = json::value::string(utility::conversions::to_base64(state.identifier));
		json["adi_pb"] = json::value::string(utility::conversions::to_base64(state.adiPB));
		std::string contents = json.serialize();

		std::string temporaryPath = path + ".tmp";
		int fd = open(temporaryPath.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
		if (fd < 0)
		{
			throw V3Error("Couldn't write " + temporaryPath + ": " + strerror(errno));
		}

		fchmod(fd, 0600);
		bool written = write(fd, contents.data(), contents.size()) == (ssize_t)contents.size() && fsync(fd) == 0;
		close(fd);

		if (!written || rename(temporaryPath.c_str(), path.c_str()) != 0)
		{
			unlink(temporaryPath.c_str());
			throw V3Error("Couldn't save " + path + ": " + strerror(errno));
		}
	}

	std::string PlistString(plist_t dictionary, const char* key)
	{
		plist_t node = dictionary != nullptr ? plist_dict_get_item(dictionary, key) : nullptr;
		if (node == nullptr || plist_get_node_type(node) != PLIST_STRING)
		{
			return "";
		}

		char* value = nullptr;
		plist_get_string_val(node, &value);
		std::string result = value != nullptr ? value : "";
		free(value);
		return result;
	}

	// Sends a provisioning request to Apple as the identity's device and returns the response plist, which
	// the caller frees. gsa.apple.com uses Apple's own CA, so like AltSign's GSA client this doesn't validate it.
	plist_t AppleRequest(method httpMethod, const std::string& url, plist_t body, const ClientInfo& clientInfo, const State& state)
	{
		uri requestURI(url);
		auto client = MakeClient(requestURI.scheme() + "://" + requestURI.host() + (requestURI.is_port_default() ? "" : ":" + std::to_string(requestURI.port())), false);

		http_request request(httpMethod);
		request.set_request_uri(requestURI.resource());

		if (body != nullptr)
		{
			char* xml = nullptr;
			uint32_t length = 0;
			plist_to_xml(body, &xml, &length);
			request.set_body(std::string(xml, length), "text/x-xml-plist");
			free(xml);
		}

		std::map<std::string, std::string> headers = {
			{ "X-Mme-Client-Info", clientInfo.clientInfo },
			{ "User-Agent", clientInfo.userAgent },
			{ "Content-Type", "text/x-xml-plist" },
			{ "X-Apple-I-MD-LU", LocalUserID(state) },
			{ "X-Mme-Device-Id", DeviceID(state) },
			{ "X-Apple-I-Client-Time", UTCTime(time(nullptr)) },
			{ "X-Apple-I-TimeZone", "UTC" },
			{ "X-Apple-Locale", "en_US" },
		};

		for (auto& pair : headers)
		{
			request.headers().remove(pair.first);
			request.headers().add(pair.first, pair.second);
		}

		auto response = client.request(request).get();
		auto data = response.extract_vector().get();

		plist_t plist = nullptr;
		if (!data.empty())
		{
			plist_from_memory((const char*)data.data(), (uint32_t)data.size(), &plist);
		}

		if (response.status_code() != status_codes::OK || plist == nullptr || plist_get_node_type(plist) != PLIST_DICT)
		{
			if (plist != nullptr)
			{
				plist_free(plist);
			}

			throw V3Error(requestURI.path() + " answered HTTP " + std::to_string(response.status_code()) + ".");
		}

		return plist;
	}

	// Apple's answer to midStartProvisioning or midFinishProvisioning: the Response dictionary's keys.
	std::map<std::string, std::string> ProvisioningResponse(const std::string& url, plist_t requestBody, std::vector<std::string> keys, const ClientInfo& clientInfo, const State& state)
	{
		plist_t plist = AppleRequest(methods::POST, url, requestBody, clientInfo, state);
		plist_t response = plist_dict_get_item(plist, "Response");

		std::map<std::string, std::string> values;
		for (auto& key : keys)
		{
			values[key] = PlistString(response, key.c_str());
		}

		plist_t status = response != nullptr ? plist_dict_get_item(response, "Status") : nullptr;
		std::string message = PlistString(status, "em");
		plist_free(plist);

		for (auto& key : keys)
		{
			if (values[key].empty())
			{
				throw V3Error("Apple's provisioning answer has no " + key + (message.empty() ? "." : ": " + message));
			}
		}

		return values;
	}

	plist_t ProvisioningBody(std::map<std::string, std::string> request)
	{
		plist_t body = plist_new_dict();
		plist_dict_set_item(body, "Header", plist_new_dict());

		plist_t requestDictionary = plist_new_dict();
		for (auto& pair : request)
		{
			plist_dict_set_item(requestDictionary, pair.first.c_str(), plist_new_string(pair.second.c_str()));
		}
		plist_dict_set_item(body, "Request", requestDictionary);

		return body;
	}

	std::string ReceiveText(web::websockets::client::websocket_client& connection)
	{
		auto task = connection.receive();

		auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
		while (!task.is_done())
		{
			if (std::chrono::steady_clock::now() > deadline)
			{
				connection.close().wait();
				throw V3Error("The provisioning session timed out.");
			}

			std::this_thread::sleep_for(std::chrono::milliseconds(50));
		}

		auto message = task.get();
		if (message.message_type() != web::websockets::client::websocket_message_type::text_message)
		{
			throw V3Error("The provisioning session ended early.");
		}

		return message.extract_string().get();
	}

	void SendJSON(web::websockets::client::websocket_client& connection, std::map<std::string, std::string> fields)
	{
		auto json = json::value::object();
		for (auto& pair : fields)
		{
			json[pair.first] = json::value::string(pair.second);
		}

		web::websockets::client::websocket_outgoing_message message;
		message.set_utf8_message(json.serialize());
		connection.send(message).wait();
	}

	void RunProvisioningSession(web::websockets::client::websocket_client& connection, const std::string& startURL, const std::string& finishURL, const ClientInfo& clientInfo, State& state);

	// Provisions the identity: the server drives Apple's midStartProvisioning/midFinishProvisioning over
	// the /v3/provisioning_session websocket, and this computer relays the Apple requests as the device.
	void Provision(const std::string& serverURL, const ClientInfo& clientInfo, State& state)
	{
		odslog("Anisette v3: provisioning device " << DeviceID(state).substr(0, 8) << " with " << serverURL << "...");

		plist_t lookup = AppleRequest(methods::GET, "https://gsa.apple.com/grandslam/GsService2/lookup", nullptr, clientInfo, state);
		plist_t urls = plist_dict_get_item(lookup, "urls");
		std::string startURL = PlistString(urls, "midStartProvisioning");
		std::string finishURL = PlistString(urls, "midFinishProvisioning");
		plist_free(lookup);

		if (startURL.empty() || finishURL.empty())
		{
			throw V3Error("Apple's lookup has no provisioning URLs.");
		}

		std::string socketURL = serverURL + "/v3/provisioning_session";
		if (socketURL.rfind("https://", 0) == 0)
		{
			socketURL.replace(0, 8, "wss://");
		}
		else if (socketURL.rfind("http://", 0) == 0)
		{
			socketURL.replace(0, 7, "ws://");
		}

		web::websockets::client::websocket_client_config config;
		if (auto proxy = EnvironmentProxy(socketURL))
		{
			config.set_proxy(*proxy);
		}

		web::websockets::client::websocket_client connection(config);
		connection.connect(socketURL).wait();

		try
		{
			RunProvisioningSession(connection, startURL, finishURL, clientInfo, state);
			connection.close().wait();
		}
		catch (...)
		{
			// Close the session on every failure, so the server isn't left waiting for its timeout.
			try
			{
				connection.close().wait();
			}
			catch (...)
			{
			}

			throw;
		}

		if (state.adiPB.empty())
		{
			throw V3Error("The server's provisioning result has no adi.pb.");
		}

		odslog("Anisette v3: provisioned device " << DeviceID(state).substr(0, 8) << ".");
	}

	// The /v3/provisioning_session conversation, until the server sends the provisioned adi.pb.
	void RunProvisioningSession(web::websockets::client::websocket_client& connection, const std::string& startURL, const std::string& finishURL, const ClientInfo& clientInfo, State& state)
	{
		while (true)
		{
			auto message = json::value::parse(ReceiveText(connection));
			std::string result = message.has_field("result") && message.at("result").is_string() ? message.at("result").as_string() : "";

			if (result == "GiveIdentifier")
			{
				SendJSON(connection, { { "identifier", utility::conversions::to_base64(state.identifier) } });
			}
			else if (result == "GiveStartProvisioningData")
			{
				plist_t body = ProvisioningBody({});
				auto response = ProvisioningResponse(startURL, body, { "spim" }, clientInfo, state);
				plist_free(body);

				SendJSON(connection, { { "spim", response["spim"] } });
			}
			else if (result == "GiveEndProvisioningData" && message.has_field("cpim") && message.at("cpim").is_string())
			{
				plist_t body = ProvisioningBody({ { "cpim", message.at("cpim").as_string() } });
				auto response = ProvisioningResponse(finishURL, body, { "ptm", "tk" }, clientInfo, state);
				plist_free(body);

				SendJSON(connection, { { "ptm", response["ptm"] }, { "tk", response["tk"] } });
			}
			else if (result == "ProvisioningSuccess" && message.has_field("adi_pb") && message.at("adi_pb").is_string())
			{
				state.adiPB = utility::conversions::from_base64(message.at("adi_pb").as_string());
				return;
			}
			else
			{
				std::string detail = message.has_field("message") && message.at("message").is_string() ? ": " + message.at("message").as_string() : "";
				throw V3Error("Provisioning failed, the server answered " + (result.empty() ? std::string("without a result") : result) + detail + ".");
			}
		}
	}

	// Headers from /v3/get_headers, or nothing when the server says the identity isn't provisioned (-45061).
	std::optional<Headers> FetchHeaders(const std::string& serverURL, const State& state)
	{
		auto body = json::value::object();
		body["identifier"] = json::value::string(utility::conversions::to_base64(state.identifier));
		body["adi_pb"] = json::value::string(utility::conversions::to_base64(state.adiPB));

		auto client = MakeClient(serverURL, true);
		auto response = client.request(methods::POST, "/v3/get_headers", body).get();

		json::value json;
		try
		{
			json = response.extract_json(true).get();
		}
		catch (json::json_exception&)
		{
			throw V3Error(serverURL + "/v3/get_headers answered HTTP " + std::to_string(response.status_code()) + " without JSON.");
		}

		std::string result = json.has_field("result") && json.at("result").is_string() ? json.at("result").as_string() : "";
		if (result == "Headers")
		{
			for (auto key : { "X-Apple-I-MD-M", "X-Apple-I-MD", "X-Apple-I-MD-RINFO" })
			{
				if (!json.has_field(key) || !json.at(key).is_string())
				{
					throw V3Error(serverURL + "/v3/get_headers answered without " + key + ".");
				}
			}

			return Headers{ json.at("X-Apple-I-MD-M").as_string(), json.at("X-Apple-I-MD").as_string(), json.at("X-Apple-I-MD-RINFO").as_string() };
		}

		std::string message = json.has_field("message") && json.at("message").is_string() ? json.at("message").as_string() : "";
		if (result == "GetHeadersError" && message.find("-45061") != std::string::npos)
		{
			return std::nullopt;
		}

		throw V3Error(serverURL + "/v3/get_headers answered HTTP " + std::to_string(response.status_code()) + " " + result + (message.empty() ? "" : ": " + message) + ".");
	}
}

bool AnisetteV3::IsSupported(const std::string& serverURL)
{
	try
	{
		FetchClientInfo(TrimmedURL(serverURL));
		return true;
	}
	catch (ServerError& error)
	{
		// The server answered, but not with v3 client info.
		return false;
	}
	catch (web::json::json_exception& exception)
	{
		return false;
	}
}

std::shared_ptr<AnisetteData> AnisetteV3::FetchAnisetteData(const std::string& serverURL, std::function<std::string(const std::string&)> clientInfoFilter)
{
	std::lock_guard<std::mutex> lock(stateMutex);

	std::string url = TrimmedURL(serverURL);

	ClientInfo clientInfo = FetchClientInfo(url);

	// Provisioning goes to Apple too, so it already sends the client info Apple will see later.
	clientInfo.clientInfo = clientInfoFilter(clientInfo.clientInfo);

	std::string path = StatePath(url);
	auto savedState = LoadState(path);

	State state;
	if (savedState.has_value())
	{
		state = *savedState;
	}
	else
	{
		state.identifier.resize(16);
		if (RAND_bytes(state.identifier.data(), (int)state.identifier.size()) != 1)
		{
			throw V3Error("Couldn't generate a random identifier for a new identity.");
		}
	}

	if (state.adiPB.empty())
	{
		Provision(url, clientInfo, state);
		SaveState(path, state);
		odslog("Anisette v3: saved the identity to " << path << ".");
	}

	auto headers = FetchHeaders(url, state);
	if (!headers.has_value())
	{
		odslog("Anisette v3: the server no longer accepts device " << DeviceID(state).substr(0, 8) << ", provisioning it again.");
		state.adiPB.clear();
		Provision(url, clientInfo, state);
		SaveState(path, state);

		headers = FetchHeaders(url, state);
		if (!headers.has_value())
		{
			throw V3Error("The server rejects the identity right after provisioning it.");
		}
	}

	odslog("Anisette v3: headers for device " << DeviceID(state).substr(0, 8) << ".");

	struct timeval now = { 0 };
	now.tv_sec = time(nullptr);

	return std::make_shared<AnisetteData>(
		headers->machineID,
		headers->oneTimePassword,
		LocalUserID(state),
		std::strtoull(headers->routingInfo.c_str(), nullptr, 10),
		DeviceID(state),
		"0",
		clientInfo.clientInfo,
		now,
		"en_US",
		"UTC");
}
