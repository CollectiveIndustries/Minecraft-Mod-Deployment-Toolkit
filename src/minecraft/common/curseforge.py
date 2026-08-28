# src/minecraft/common/curseforge.py
"""CurseForge API client functions."""

import requests

CURSEFORGE_GAME_ID = 432  # Minecraft


def curseforge_request(
    endpoint: str, api_key: str, method="GET", params=None, json_data=None
):
    """Make a request to the CurseForge API with required headers."""
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    url = f"https://api.curseforge.com{endpoint}"
    resp = requests.request(
        method, url, headers=headers, params=params, json=json_data, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def get_download_url_by_ids(project_id: int, file_id: int, api_key: str) -> str:
    """Get direct download URL for a specific file."""
    resp = curseforge_request(
        f"/v1/mods/{project_id}/files/{file_id}/download-url", api_key
    )
    return resp["data"]


def fetch_all_files(mod_id: int, api_key: str, max_pages=5, page_size=100) -> list:
    """Retrieve all files for a mod, handling pagination."""
    all_files = []
    index = 0
    for _ in range(max_pages):
        resp = curseforge_request(
            f"/v1/mods/{mod_id}/files",
            api_key,
            params={"pageSize": page_size, "index": index},
        )
        page = resp.get("data", [])
        if not page:
            break
        all_files.extend(page)
        pagination = resp.get("pagination", {})
        total = pagination.get("totalCount", 0)
        if index + page_size >= total:
            break
        index += page_size
    return all_files


def find_mod_by_slug(slug: str, api_key: str) -> dict | None:
    """Find a mod by exact slug. Returns the mod dict or None."""
    try:
        resp = curseforge_request(
            "/v1/mods/search",
            api_key,
            params={"gameId": CURSEFORGE_GAME_ID, "slug": slug, "pageSize": 1},
        )
        data = resp.get("data", [])
        if data:
            return data[0]
    except requests.RequestException:
        # Let caller handle; logging would be done upstream.
        pass
    return None


def get_mod_file_url_by_slug(
    slug: str, version: str, minecraft_version: str, api_key: str
):
    """
    Fallback method: search by slug, fetch files, and find a matching file
    by version string or file name. Returns (download_url, file_name).
    """
    # 1. Find mod by slug
    mod = find_mod_by_slug(slug, api_key)
    if not mod:
        # Try searchFilter as last resort
        resp = curseforge_request(
            "/v1/mods/search",
            api_key,
            params={"gameId": CURSEFORGE_GAME_ID, "searchFilter": slug, "pageSize": 1},
        )
        data = resp.get("data", [])
        if not data:
            raise ValueError(f"Mod with slug '{slug}' not found")
        mod = data[0]

    mod_id = mod["id"]

    # 2. Get all files
    all_files = fetch_all_files(mod_id, api_key)
    if not all_files:
        raise ValueError(f"No files found for mod '{slug}'")

    # 3. Filter by Minecraft version (1.20.1 or 1.20)
    filtered = [
        f
        for f in all_files
        if any("1.20.1" in gv or "1.20" in gv for gv in f.get("gameVersions", []))
    ]
    if not filtered:
        filtered = all_files  # fallback

    # 4. Match by version string in fileName or displayName
    matched = None
    for f in filtered:
        fname = f.get("fileName", "")
        display = f.get("displayName", "")
        if version in fname or version in display:
            matched = f
            break

    # 5. If not found, try exact filename match (case-insensitive)
    if not matched:
        jar_name = f"{slug}.jar"
        for f in filtered:
            if f.get("fileName", "").lower() == jar_name.lower():
                matched = f
                break

    # 6. Fallback to latest file
    if not matched and filtered:
        matched = max(filtered, key=lambda f: f.get("fileDate", ""))

    if not matched:
        raise ValueError(
            f"No matching file for '{slug}' version '{version}' on MC {minecraft_version}"
        )

    # 7. Get download URL
    download_url = get_download_url_by_ids(mod_id, matched["id"], api_key)
    return download_url, matched["fileName"]
