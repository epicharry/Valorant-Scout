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


def get_mmr(auth_token, ent_token, puuid, shard, client_version):
    url = f"https://pd.{shard}.a.pvp.net/mmr/v1/players/{puuid}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        "X-Riot-ClientVersion": client_version,
        "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
    }
    r = requests.get(url, headers=headers, verify=False, timeout=8)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json(), r.text


if __name__ == "__main__":
    try:
        lockfile = get_lockfile()
        client_version = get_client_version(lockfile)
        auth_token, ent_token, local_puuid = fetch_tokens(lockfile)
        shard = get_region_from_log()

        print(f"Region detected: {shard}")
        print(f"Client version: {client_version}")
        print(f"Your PUUID: {local_puuid}")

        target_puuid = input("\nEnter target PUUID (press Enter to use your own): ").strip()
        if not target_puuid:
            target_puuid = local_puuid

        mmr_json, raw_mmr = get_mmr(auth_token, ent_token, target_puuid, shard, client_version)

        filename = f"mmr_{target_puuid[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(mmr_json, f, indent=2)

        print(f"\nMMR data saved to {filename}")

        if "QueueSkills" in mmr_json:
            comp = (mmr_json["QueueSkills"].get("competitive") or {})
            seasonal = comp.get("SeasonalInfoBySeasonID") or {}
            print(f"\nSeasons with data: {len(seasonal)}")
            for season_id, sdata in list(seasonal.items())[-3:]:
                tier = sdata.get("CompetitiveTier", 0)
                rr = sdata.get("RankedRating", 0)
                wins = sdata.get("NumberOfWinsWithPlacements", 0)
                games = sdata.get("NumberOfGames", 0)
                print(f"  Season {season_id[:8]}... | Tier {tier} | RR {rr} | "
                      f"Wins {wins}/{games}")
        else:
            print("\nNo QueueSkills in response:")
            print(json.dumps(mmr_json, indent=2)[:500])

    except FileNotFoundError:
        print("Error: Riot Client lockfile not found. Make sure the game client is running.")
    except Exception as e:
        print(f"Error: {e}")
