#include "DevicePairingManager.h"

#include <iostream>
#include <memory>
#include <optional>
#include <stdlib.h>
#include <string.h>
#include <netdb.h>
#include <sys/socket.h>
#include <pthread.h>
#include <exception>
#include <functional>
#include <mutex>
#include <set>

#include "ServerError.hpp"

// idevice.h also declares plist_* functions from idevice's own plist implementation, which clash with
// libplist's declarations, so this is the only file that includes it. The build prelinks idevice into
// a single object whose only global symbols are the functions listed in makefiles/idevice-build/idevice-api.txt.
extern "C" {
#include "ffi/idevice.h"
}

namespace
{
	std::string FFIErrorDescription(IdeviceFfiError* error)
	{
		if (error == NULL)
		{
			return "";
		}

		std::string description = "idevice error " + std::to_string(error->code);
		if (error->message != NULL)
		{
			description += ": " + std::string(error->message);
		}

		idevice_error_free(error);
		return description;
	}

	// Same messages as RemotePairingError in AltServer for macOS.
	ServerError PairingError(ServerErrorCode code, std::string failureReason, std::string recoverySuggestion, IdeviceFfiError* ffiError)
	{
		auto ffiDescription = FFIErrorDescription(ffiError);
		std::cout << "Failed to generate pairing file. " << failureReason;
		if (!ffiDescription.empty())
		{
			std::cout << " (" << ffiDescription << ")";
		}
		std::cout << std::endl;

		return ServerError(code, {
			{ "NSLocalizedFailureReason", failureReason },
			{ "NSLocalizedRecoverySuggestion", recoverySuggestion },
		});
	}

	ServerError UnknownPairingError(std::string reason, IdeviceFfiError* ffiError = NULL)
	{
		std::cout << "Remote AltServer setup failed: " << reason << std::endl;
		return PairingError(ServerErrorCode::Unknown,
			"Remote AltServer couldn't be configured for this device.",
			"Try again. If the problem continues, reconnect your device or restart your computer.",
			ffiError);
	}

	// Where usbmuxd listens. The rest of AltServer reaches usbmuxd through libusbmuxd, so this reads
	// USBMUXD_SOCKET_ADDRESS by libusbmuxd's rules and pairing uses the same usbmuxd:
	//   UNIX:<path>                        the unix socket at <path>
	//   <host>:<port> or [<host>]:<port>   TCP, where <host> may be a name
	//   unset, or any other value          the default socket, /var/run/usbmuxd
	// idevice's own reader (idevice_usbmuxd_new_default_connection) takes every value that contains a
	// ':' as a numeric TCP address, so it rejects UNIX:<path>. The macOS version always uses the
	// default socket.
	struct UsbmuxdLocation
	{
		std::string socketPath; // Empty for TCP.
		sockaddr_storage tcpAddress = {};
		socklen_t tcpAddressLength = 0;
		std::string description;
	};

	UsbmuxdLocation ReadUsbmuxdLocation()
	{
		UsbmuxdLocation location;
		location.socketPath = "/var/run/usbmuxd";
		location.description = location.socketPath;

		const char* value = getenv("USBMUXD_SOCKET_ADDRESS");
		if (value == NULL)
		{
			return location;
		}

		std::string address = value;
		if (address.compare(0, 5, "UNIX:") == 0)
		{
			if (address.size() > 5)
			{
				location.socketPath = address.substr(5);
				location.description = location.socketPath;
			}
			return location;
		}

		auto separator = address.rfind(':');
		if (separator == std::string::npos)
		{
			return location;
		}

		// Like libusbmuxd: the whole text after the last ':' must be a port from 1 to 65535.
		std::string port = address.substr(separator + 1);
		char* portEnd = NULL;
		long portNumber = strtol(port.c_str(), &portEnd, 10);
		if (*portEnd != '\0' || portNumber < 1 || portNumber > 65535)
		{
			return location;
		}

		std::string host = address.substr(0, separator);
		if (!host.empty() && host.front() == '[')
		{
			host = host.substr(1);
			auto bracket = host.rfind(']');
			if (bracket != std::string::npos)
			{
				host = host.substr(0, bracket);
			}
		}

		if (host.empty())
		{
			return location;
		}

		struct addrinfo hints = {};
		hints.ai_family = AF_UNSPEC;
		hints.ai_socktype = SOCK_STREAM;

		struct addrinfo* result = NULL;
		int status = getaddrinfo(host.c_str(), std::to_string(portNumber).c_str(), &hints, &result);
		if (status != 0 || result == NULL)
		{
			throw UnknownPairingError("Couldn't resolve USBMUXD_SOCKET_ADDRESS " + address + ": " + gai_strerror(status));
		}

		memcpy(&location.tcpAddress, result->ai_addr, result->ai_addrlen);
		location.tcpAddressLength = result->ai_addrlen;
		freeaddrinfo(result);

		location.socketPath.clear();
		location.description = address;
		return location;
	}

	UsbmuxdConnectionHandle* ConnectToUsbmuxd(const UsbmuxdLocation& location)
	{
		UsbmuxdConnectionHandle* connection = NULL;
		IdeviceFfiError* error = NULL;
		if (location.socketPath.empty())
		{
			error = idevice_usbmuxd_new_tcp_connection((const idevice_sockaddr*)&location.tcpAddress, location.tcpAddressLength, 0, &connection);
		}
		else
		{
			error = idevice_usbmuxd_new_unix_socket_connection(location.socketPath.c_str(), 0, &connection);
		}

		if (error != NULL)
		{
			throw UnknownPairingError("Couldn't connect to usbmuxd at " + location.description + ".", error);
		}

		return connection;
	}

	UsbmuxdAddrHandle* CreateUsbmuxdAddress(const UsbmuxdLocation& location)
	{
		UsbmuxdAddrHandle* address = NULL;
		IdeviceFfiError* error = NULL;
		if (location.socketPath.empty())
		{
			error = idevice_usbmuxd_tcp_addr_new((const idevice_sockaddr*)&location.tcpAddress, location.tcpAddressLength, &address);
		}
		else
		{
			error = idevice_usbmuxd_unix_addr_new(location.socketPath.c_str(), &address);
		}

		if (error != NULL)
		{
			throw UnknownPairingError("Couldn't create usbmuxd address for " + location.description + ".", error);
		}

		return address;
	}

	// The value idevice_usbmuxd_device_get_connection_type() returns for a USB connection. It is
	// idevice's UsbmuxdConnectionType::Usb, which idevice.h does not declare.
	const uint8_t UsbmuxdConnectionTypeUSB = 1;

	// Finds the usbmuxd device_id of this UDID's USB connection. usbmuxd also lists a device that it
	// reaches over the network, under the same UDID, and pairing must not use that entry.
	uint32_t ResolveDeviceID(std::string udid, const UsbmuxdLocation& location)
	{
		std::unique_ptr<UsbmuxdConnectionHandle, decltype(&idevice_usbmuxd_connection_free)> muxConnection(ConnectToUsbmuxd(location), idevice_usbmuxd_connection_free);

		UsbmuxdDeviceHandle** devices = NULL;
		int deviceCount = 0;
		if (auto error = idevice_usbmuxd_get_devices(muxConnection.get(), &devices, &deviceCount))
		{
			throw UnknownPairingError("Couldn't query connected devices.", error);
		}

		std::optional<uint32_t> deviceID;
		for (int i = 0; i < deviceCount; i++)
		{
			if (idevice_usbmuxd_device_get_connection_type(devices[i]) != UsbmuxdConnectionTypeUSB)
			{
				continue;
			}

			char* deviceUDID = idevice_usbmuxd_device_get_udid(devices[i]);
			if (deviceUDID == NULL)
			{
				continue;
			}

			if (udid == deviceUDID)
			{
				deviceID = idevice_usbmuxd_device_get_device_id(devices[i]);
			}

			idevice_string_free(deviceUDID);

			if (deviceID.has_value())
			{
				break;
			}
		}

		idevice_usbmuxd_device_list_free(devices, deviceCount);

		if (!deviceID.has_value())
		{
			throw PairingError(ServerErrorCode::DeviceNotFound,
				"This device isn't connected to AltServer via USB.",
				"Connect your device to this computer with a cable, then try again.",
				NULL);
		}

		return *deviceID;
	}

	std::vector<unsigned char> PairAndSerialize(std::string udid, std::string hostName)
	{
		// 1. Resolve the device_id (required by idevice) of this UDID's USB connection.
		UsbmuxdLocation location = ReadUsbmuxdLocation();
		uint32_t deviceID = ResolveDeviceID(udid, location);

		// 2. Build a device provider for the same usbmuxd. usbmuxd_provider_new() takes ownership of the address only when it succeeds.
		UsbmuxdAddrHandle* address = CreateUsbmuxdAddress(location);

		IdeviceProviderHandle* provider = NULL;
		if (auto error = usbmuxd_provider_new(address, 0, udid.c_str(), deviceID, hostName.c_str(), &provider))
		{
			idevice_usbmuxd_addr_free(address);
			throw UnknownPairingError("Couldn't create device provider.", error);
		}
		std::unique_ptr<IdeviceProviderHandle, decltype(&idevice_provider_free)> providerOwner(provider, idevice_provider_free);

		// 3. Pair. A NULL pin_callback makes idevice use "000000". Triggers the on-device "Trust" prompt.
		RpPairingFileHandle* pairingFile = NULL;
		if (auto error = tunnel_pair_usb(provider, hostName.c_str(), NULL, NULL, &pairingFile))
		{
			throw PairingError(ServerErrorCode::ConnectionFailed,
				"AltServer couldn't pair with this device.",
				"Make sure your device is unlocked, then tap Trust when prompted.",
				error);
		}
		std::unique_ptr<RpPairingFileHandle, decltype(&rp_pairing_file_free)> pairingFileOwner(pairingFile, rp_pairing_file_free);

		// 4. Serialize the plist.
		uint8_t* bytes = NULL;
		uintptr_t length = 0;
		if (auto error = rp_pairing_file_to_bytes(pairingFile, &bytes, &length))
		{
			throw UnknownPairingError("Couldn't serialize pairing file.", error);
		}

		if (bytes == NULL || length == 0)
		{
			throw UnknownPairingError("Pairing file serialized to empty data.");
		}

		std::vector<unsigned char> data(bytes, bytes + length);
		idevice_data_free(bytes, length);

		return data;
	}

	// Marks a pairing with one device as running, for as long as the object lives. tunnel_pair_usb()
	// returns only when the user answers the Trust prompt, the device disconnects or the connection
	// fails, and idevice has no call that cancels it. So a retry while the prompt is still up would
	// start a second pairing with the same device and block one more request thread and 8 MiB stack.
	// A second request for the same UDID fails at once instead.
	class PairingInProgress
	{
	public:
		PairingInProgress(std::string udid) : udid(udid)
		{
			std::lock_guard<std::mutex> lock(mutex());
			if (!udids().insert(udid).second)
			{
				throw PairingError(ServerErrorCode::Unknown,
					"AltServer is already pairing with this device.",
					"Tap Trust or Don't Trust on your device, or disconnect it, then try again.",
					NULL);
			}
		}

		~PairingInProgress()
		{
			std::lock_guard<std::mutex> lock(mutex());
			udids().erase(udid);
		}

		PairingInProgress(const PairingInProgress&) = delete;
		PairingInProgress& operator=(const PairingInProgress&) = delete;

	private:
		std::string udid;

		static std::mutex& mutex()
		{
			static std::mutex mutex;
			return mutex;
		}

		static std::set<std::string>& udids()
		{
			static std::set<std::string> udids;
			return udids;
		}
	};

	// Runs work on a new thread with an 8 MiB stack and returns its result or rethrows its exception.
	// tunnel_pair_usb() polls idevice's pairing future on the calling thread, and that poll needs more
	// than the 128 KiB that musl gives threads by default, including cpprestsdk's thread pool threads.
	// The kernel only backs the stack pages the thread touches.
	std::vector<unsigned char> RunOnLargeStack(std::function<std::vector<unsigned char>()> work)
	{
		struct Context
		{
			std::function<std::vector<unsigned char>()> work;
			std::vector<unsigned char> result;
			std::exception_ptr exception;
		} context = { work, {}, nullptr };

		auto threadMain = [](void* argument) -> void* {
			auto context = static_cast<Context*>(argument);
			try
			{
				context->result = context->work();
			}
			catch (...)
			{
				context->exception = std::current_exception();
			}
			return NULL;
		};

		pthread_attr_t attributes;
		pthread_attr_init(&attributes);
		pthread_attr_setstacksize(&attributes, 8 * 1024 * 1024);

		pthread_t thread;
		int result = pthread_create(&thread, &attributes, threadMain, &context);
		pthread_attr_destroy(&attributes);

		if (result != 0)
		{
			throw UnknownPairingError("Couldn't start pairing thread: " + std::string(strerror(result)));
		}

		pthread_join(thread, NULL);

		if (context.exception)
		{
			std::rethrow_exception(context.exception);
		}

		return context.result;
	}
}

DevicePairingManager* DevicePairingManager::instance()
{
	static DevicePairingManager instance;
	return &instance;
}

std::vector<unsigned char> DevicePairingManager::GeneratePairingFile(std::string udid, std::string hostName)
{
	PairingInProgress pairing(udid);
	return RunOnLargeStack([udid, hostName]() { return PairAndSerialize(udid, hostName); });
}
