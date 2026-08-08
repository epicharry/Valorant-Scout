import os
import json
import base64
import requests
from datetime import datetime

requests.packages.urllib3.disable_warnings()

CLIENT_PLATFORM = "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjogIldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
CLIENT_VERSION = "release-11.02-25-3708969"
DEFAULT_REGION = "ap"  # default AP

def fetch_tokens():
    """Read Riot lockfile and fetch access/entitlement tokens + local puuid"""
    lockfile_path = os.path.join(os.getenv('LOCALAPPDATA'),
                                 "Riot Games", "Riot Client", "Config", "lockfile")
    with open(lockfile_path, "r") as f:
        name, pid, port, password, protocol = f.read().split(":")
    auth_header = base64.b64encode(f"riot:{password}".encode()).decode()
    base_url = f"{protocol}://127.0.0.1:{port}"
    r = requests.get(f"{base_url}/entitlements/v1/token",
                     headers={"Authorization": f"Basic {auth_header}"},
                     verify=False)
    tokens = r.json()
    auth_token = tokens["accessToken"]
    ent_token = tokens["token"]
    puuid = json.loads(base64.b64decode(auth_token.split('.')[1] + "=="))["sub"]
    return auth_token, ent_token, puuid

def get_mmr(auth_token, ent_token, puuid, shard=DEFAULT_REGION):
    """Fetch MMR data for a player"""
    url = f"https://pd.{shard}.a.pvp.net/mmr/v1/players/{puuid}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        "X-Riot-ClientVersion": CLIENT_VERSION
    }
    r = requests.get(url, headers=headers, verify=False)
    return r.json(), r.text

if __name__ == "__main__":
    try:
        auth_token, ent_token, local_puuid = fetch_tokens()

        target_puuid = input("Enter target PUUID (press Enter to use your own): ").strip()
        if not target_puuid:
            target_puuid = local_puuid
            print(f"Using your own PUUID: {target_puuid}")

        region = input(f"Enter region (default: {DEFAULT_REGION}): ").strip()
        if not region:
            region = DEFAULT_REGION

        mmr_json, raw_mmr = get_mmr(auth_token, ent_token, target_puuid, region)

        # save with timestamp
        filename = f"mmr_{target_puuid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(raw_mmr)

        print(f"MMR data saved to {filename}")
        
        # Pretty print summary of MMR data
        if mmr_json:
            print("\n--- MMR Summary ---")
            if "QueueSkills" in mmr_json:
                for queue, data in mmr_json["QueueSkills"].items():
                    if "SeasonalInfoBySeasonID" in data:
                        for season_id, season_data in data["SeasonalInfoBySeasonID"].items():
                            print(f"Queue: {queue}, Season: {season_id}")
                            if "CompetitiveRanking" in season_data:
                                print(f"  Ranking: {season_data['CompetitiveRanking']}")
                            if "RankedRating" in season_data:
                                print(f"  Ranked Rating: {season_data['RankedRating']}")
                            if "GamesNeededForRating" in season_data:
                                print(f"  Games Needed for Rating: {season_data['GamesNeededForRating']}")
                            print()
            else:
                print("No QueueSkills data found")
                print(json.dumps(mmr_json, indent=2)[:500] + "...")

    except Exception as e:
        print(f"Error: {e}")
        print("Make sure Riot Client is running and you are logged in.")