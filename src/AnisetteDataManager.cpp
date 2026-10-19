#include "AnisetteDataManager.h"
#include <sstream>
#include <filesystem>
#include "Error.hpp"
#include "ServerError.hpp"

#include <set>
#include <ctime>
#include <cstdlib>
#include <cstring>
#include <cctype>

#include "AnisetteData.h"
#include "AltServerApp.h"

#define odslog(msg) { std::stringstream ss; ss << msg << std::endl; OutputDebugStringA(ss.str().c_str()); }

AnisetteDataManager* AnisetteDataManager::_instance = nullptr;

AnisetteDataManager* AnisetteDataManager::instance()
{
	if (_instance == 0)
	{
		_instance = new AnisetteDataManager();
	}

	return _instance;
}

AnisetteDataManager::AnisetteDataManager() : loadedDependencies(false)
{
}

AnisetteDataManager::~AnisetteDataManager()
{
}

bool AnisetteDataManager::LoadiCloudDependencies()
{
	return true;
}

bool AnisetteDataManager::LoadDependencies()
{
	return true;
}

#include <cpprest/json.h>

using namespace web;                        // Common features like URIs.
using namespace web::http;                  // Common HTTP functionality
using namespace web::http::client;          // HTTP client features

std::string GetAnisetteURL() {
	const char *server = getenv("ALTSERVER_ANISETTE_SERVER");
	if (server) {
		return server;
	}
	return U("https://armconverter.com/anisette/irGb3Quww8zrhgqnzmrx");
}

// Apple rejects any gsa.apple.com request whose X-Mme-Client-Info (aka
// "client info" / device description) header contains the substring
// "com.apple.dt.Xcode" with a 503. Replace every occurrence with
// "com.apple.akd" so no AnisetteData built from this manager ever leaks
// that string to Apple, regardless of what the anisette server returns.
std::string SanitizeClientInfo(const std::string& clientInfo)
{
	static const std::string needle = "com.apple.dt.Xcode";
	static const std::string replacement = "com.apple.akd";

	std::string result = clientInfo;
	size_t pos = 0;
	while ((pos = result.find(needle, pos)) != std::string::npos)
	{
		result.replace(pos, needle.length(), replacement);
		pos += replacement.length();
	}

	return result;
}

std::shared_ptr<AnisetteData> AnisetteDataManager::FetchAnisetteData()
{
	// auto client = web::http::client::http_client(U("https://armconverter.com"));
	// std::string wideURI = ("/anisette/irGb3Quww8zrhgqnzmrx");
	
	// auto encodedURI = web::uri::encode_uri(wideURI);
	// uri_builder builder(encodedURI);

	// http_request request(methods::GET);
	// request.set_request_uri(builder.to_string());

	auto client = web::http::client::http_client(GetAnisetteURL());
	http_request request(methods::GET);
	
	std::map<utility::string_t, utility::string_t> headers = {
		{"User-Agent", "Xcode"},
	};

	for (auto& pair : headers)
	{
		if (request.headers().has(pair.first))
		{
			request.headers().remove(pair.first);
		}

		request.headers().add(pair.first, pair.second);
	}

	std::shared_ptr<AnisetteData> anisetteData = NULL;

	auto task = client.request(request)
		.then([=](http_response response)
			{
				return response.content_ready();
			})
		.then([=](http_response response)
			{
				odslog("Received response status code: " << response.status_code());
				return response.extract_json();
			})
		.then([&anisetteData](pplx::task<json::value> previousTask)
			{
				odslog("parse anisette data ret");
				json::value jsonVal = previousTask.get();
				odslog("Got anisetteData json: " << jsonVal);
				std::vector<std::string> keys = {
					"X-Apple-I-MD-M",
					"X-Apple-I-MD",
					"X-Apple-I-MD-LU",
					"X-Apple-I-MD-RINFO",
					"X-Mme-Device-Id",
					"X-Apple-I-SRL-NO",
					"X-MMe-Client-Info",
					"X-Apple-I-Client-Time",
					"X-Apple-Locale",
					"X-Apple-I-TimeZone"
				};
				for (auto &key : keys) {
					odslog(key << ": " << jsonVal.at(key).as_string().c_str());
				}

				// X-Apple-I-Client-Time is UTC, for example 2026-09-26T12:34:56Z. Some anisette servers add
				// fractional seconds or leave out the Z; both name the same UTC instant. Any other value,
				// including a numeric UTC offset that timegm() would ignore, is rejected: it would
				// otherwise be sent to Apple as a wrong client time.
				std::string clientTime = jsonVal.at("X-Apple-I-Client-Time").as_string();
				struct tm tm = { 0 };
				const char* tail = strptime(clientTime.c_str(), "%Y-%m-%dT%H:%M:%S", &tm);
				if (tail != NULL && *tail == '.')
				{
					do
					{
						tail++;
					} while (isdigit((unsigned char)*tail));
				}

				if (tail == NULL || (*tail != '\0' && strcmp(tail, "Z") != 0))
				{
					odslog("Invalid X-Apple-I-Client-Time from the anisette server: " << clientTime);
					throw ServerError(ServerErrorCode::InvalidAnisetteData, {
						{ LocalizedFailureErrorKey, "The anisette server returned an X-Apple-I-Client-Time that is not a UTC time of the form YYYY-MM-DDTHH:MM:SSZ: " + clientTime }
					});
				}

				unsigned long ts = timegm(&tm); // X-Apple-I-Client-Time is UTC; mktime() would read it as local time
				struct timeval tv = { 0 };
				tv.tv_sec = ts;
				tv.tv_usec = 0;

				std::string sanitizedClientInfo = SanitizeClientInfo(jsonVal.at("X-MMe-Client-Info").as_string());
				odslog("Sanitized client info: " << sanitizedClientInfo);

				odslog("Building anisetteData obj...");
				anisetteData = std::make_shared<AnisetteData>(
					jsonVal.at("X-Apple-I-MD-M").as_string(),
					jsonVal.at("X-Apple-I-MD").as_string(),
					jsonVal.at("X-Apple-I-MD-LU").as_string(),
					std::atoi(jsonVal.at("X-Apple-I-MD-RINFO").as_string().c_str()),
					jsonVal.at("X-Mme-Device-Id").as_string(),
					jsonVal.at("X-Apple-I-SRL-NO").as_string(),
					sanitizedClientInfo,
					tv,
					jsonVal.at("X-Apple-Locale").as_string(),
					jsonVal.at("X-Apple-I-TimeZone").as_string());
				
				//IterateJSONValue();
			});
	
	task.wait();

	odslog(*anisetteData);

	return anisetteData;
}

bool AnisetteDataManager::ReprovisionDevice(std::function<void(void)> provisionCallback)
{
#if !SPOOF_MAC
	provisionCallback();
	return true;
#else
	std::string adiDirectoryPath = "C:\\ProgramData\\Apple Computer\\iTunes\\adi";

	/* Start Provisioning */

	// Move iCloud's ADI files (so we don't mess with them).
	for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
	{
		if (entry.path().extension() == ".pb")
		{
			fs::path backupPath = entry.path();
			backupPath += ".icloud";

			fs::rename(entry.path(), backupPath);
		}
	}

	// Copy existing AltServer .pb files into original location to reuse the MID.
	for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
	{
		if (entry.path().extension() == ".altserver")
		{
			fs::path path = entry.path();
			path.replace_extension();

			fs::rename(entry.path(), path);
		}
	}

	auto cleanUp = [adiDirectoryPath]() {
		/* Finish Provisioning */

		// Backup AltServer ADI files.
		for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
		{
			// Backup AltStore file
			if (entry.path().extension() == ".pb")
			{
				fs::path backupPath = entry.path();
				backupPath += ".altserver";

				fs::rename(entry.path(), backupPath);
			}
		}

		// Copy iCloud ADI files back to original location.
		for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
		{
			if (entry.path().extension() == ".icloud")
			{
				// Move backup file to original location
				fs::path path = entry.path();
				path.replace_extension();

				fs::rename(entry.path(), path);

				odslog("Copying iCloud file from: " << entry.path().string() << " to: " << path.string());
			}
		}
	};

	// Calling CopyAnisetteData implicitly generates new anisette data,
	// using the new client info string we injected.
	ObjcObject* error = NULL;
	ObjcObject* anisetteDictionary = (ObjcObject*)CopyAnisetteData(NULL, 0x1, &error);

	try
	{
		if (anisetteDictionary == NULL)
		{
			odslog("Reprovision Error:" << ((ObjcObject*)error)->description());

			ObjcObject* localizedDescription = (ObjcObject*)((id(*)(id, SEL))objc_msgSend)(error, sel_registerName("localizedDescription"));
			if (localizedDescription)
			{
				int errorCode = ((int(*)(id, SEL))objc_msgSend)(error, sel_registerName("code"));
				throw LocalizedError(errorCode, localizedDescription->description());
			}
			else
			{
				throw ServerError(ServerErrorCode::InvalidAnisetteData);
			}
		}

		odslog("Reprovisioned Anisette:" << anisetteDictionary->description());

		AltServerApp::instance()->setReprovisionedDevice(true);

		// Call callback while machine is provisioned for AltServer.
		provisionCallback();
	}
	catch (std::exception &exception)
	{
		cleanUp();

		throw;
	}

	cleanUp();

	return true;
#endif
}

bool AnisetteDataManager::ResetProvisioning()
{
	std::string adiDirectoryPath = "C:\\ProgramData\\Apple Computer\\iTunes\\adi";

	// Remove existing AltServer .pb files so we can create new ones next time we provision this device.
	for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
	{
		if (entry.path().extension() == ".altserver")
		{
			fs::remove(entry.path());
		}
	}

	return true;
}