import os
import json
import base64
import requests
from datetime import datetime

requests.packages.urllib3.disable_warnings()

CLIENT_PLATFORM = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
    "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
    "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
)


def get_lockfile() -> dict:
    path = os.path.join(os.getenv("LOCALAPPDATA", ""),
                        r"Riot Games\Riot Client\Config\lockfile")
    with open(path, encoding="utf-8") as f:
        keys = ["name", "PID", "port", "password", "protocol"]
        return dict(zip(keys, f.read().split(":")))


def get_region_from_log():
    path = os.path.join(os.getenv("LOCALAPPDATA", ""),
                        r"VALORANT\Saved\Logs\ShooterGame.log")
    shard = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if ".a.pvp.net/account-xp/v1/" in line:
                shard = line.split(".a.pvp.net/account-xp/v1/")[0].split(".")[-1]
                break
    return shard or "ap"


def get_client_version(lockfile: dict) -> str:
    local = {"Authorization": "Basic " + base64.b64encode(
        ("riot:" + lockfile["password"]).encode()).decode()}
    try:
        data = requests.get(
            f"https://127.0.0.1:{lockfile['port']}/chat/v4/presences",
            headers=local, verify=False, timeout=5).json()
        for pr in (data or {}).get("presences", []) or []:
            if pr.get("product") != "valorant" or not pr.get("private"):
                continue
            try:
                priv = json.loads(base64.b64decode(str(pr["private"])).decode("utf-8"))
            except Exception:
                continue
            v = (priv.get("partyPresenceData") or {}).get("partyClientVersion") \
                or priv.get("partyClientVersion")
            if v:
                return v
    except Exception:
        pass
    try:
        data = requests.get("https://valorant-api.com/v1/version", timeout=6).json()
        rcv = (data.get("data") or {}).get("riotClientVersion")
        if rcv:
            return rcv
    except Exception:
        pass
    return "release-09.00"


def fetch_tokens(lockfile: dict) -> tuple[str, str, str]:
    local = {"Authorization": "Basic " + base64.b64encode(
        ("riot:" + lockfile["password"]).encode()).decode()}
    resp = requests.get(
        f"https://127.0.0.1:{lockfile['port']}/entitlements/v1/token",
        headers=local, verify=False, timeout=5)
    ent = resp.json()
    if not isinstance(ent, dict) or "accessToken" not in ent:
        raise RuntimeError(f"Entitlements not ready (HTTP {resp.status_code})")
    return ent["accessToken"], ent["token"], ent["subject"]


def build_headers(auth_token, ent_token, client_version):
    return {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        "X-Riot-ClientVersion": client_version,
        "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
    }


def pd_get(url, headers):
    r = requests.get(url, headers=headers, verify=False, timeout=10)
    print(f"  [{r.status_code}] {url}")
    try:
        return r.json()
    except ValueError:
        return {"_raw_text": r.text, "_status_code": r.status_code}


if __name__ == "__main__":
    try:
        lockfile = get_lockfile()
        client_version = get_client_version(lockfile)
        auth_token, ent_token, puuid = fetch_tokens(lockfile)
        shard = get_region_from_log()
        headers = build_headers(auth_token, ent_token, client_version)

        pd_base = f"https://pd.{shard}.a.pvp.net"

        print(f"Region: {shard}")
        print(f"Client version: {client_version}")
        print(f"PUUID: {puuid}")
        print(f"\nFetching Premier endpoints...\n")

        endpoints = {
            "Premier_GetPlayer_V2": f"/premier/v2/players/{puuid}",
            "Premier_GetEligibility": "/premier/v1/player/eligibility",
            "Premier_FetchPremierSeasons": f"/premier/v1/affinities/{shard}/premier-seasons",
            "Premier_GetActivePremierSeason": f"/premier/v1/affinities/{shard}/premier-seasons/active",
            "Premier_GetPremierConferences": f"/premier/v1/affinities/{shard}/conferences",
        }

        results = {}
        for name, path in endpoints.items():
            url = pd_base + path
            results[name] = pd_get(url, headers)

        output = {
            "_meta": {
                "puuid": puuid,
                "shard": shard,
                "client_version": client_version,
                "timestamp": datetime.now().isoformat(),
            },
            "responses": results,
        }

        filename = f"premier_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)

        print(f"\nAll responses saved to {filename}")

        print("\n--- Summary ---")
        for name, data in results.items():
            if isinstance(data, dict):
                keys = [k for k in data.keys() if not k.startswith("_")]
                preview = ", ".join(keys[:5])
                if len(keys) > 5:
                    preview += f", ... (+{len(keys) - 5} more)"
                print(f"  {name}: {{{preview}}}")
            else:
                print(f"  {name}: {type(data).__name__}")

        print("\nNote: Premier_CreateRoster_V2 (POST) was skipped to avoid "
              "modifying your account.")

    except FileNotFoundError:
        print("Error: Riot Client lockfile not found. Make sure the game client is running.")
    except Exception as e:
        print(f"Error: {e}")
