#pragma once

#include <functional>
#include <memory>
#include <string>

class AnisetteData;

// Client for anisette v3 servers (SideStore's anisette-v3-server protocol, as omnisette's
// remote_anisette_v3.rs and Sideloader speak it). The server provisions an identity once, and its
// keychain identifier and adi.pb stay on this computer, so Apple sees the same device at every
// sign-in instead of a new one each time.
namespace AnisetteV3
{
	// Whether serverURL answers GET /v3/client_info. A server that answers without v3 support gives false;
	// a server that can't be reached throws, so a network problem doesn't quietly select v1.
	bool IsSupported(const std::string& serverURL);

	// Anisette data for this computer's identity on serverURL, provisioned first if there is none yet.
	// clientInfoFilter turns the server's client info into the X-Mme-Client-Info sent to Apple.
	std::shared_ptr<AnisetteData> FetchAnisetteData(const std::string& serverURL, std::function<std::string(const std::string&)> clientInfoFilter);
}
