import logging
import hubscape_adk
import difflib
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

async def switch_hub(hubId: str) -> dict:
    """Navigates to or switches the active workspace to the specified hub name or ID.
    
    Args:
        hubId: The ID or name of the target hub to switch to.
    """
    return await _switch_hub_impl(hubId)

switchHub = switch_hub

async def _switch_hub_impl(hubId: str) -> dict:
    context = hubscape_adk.get_context()
    user_id = context.auth.user_id
    logger.info(f"[find-hub] Executing switchHub tool: hubId='{hubId}', user_id='{user_id}'")

    resolved_id = hubId
    
    # 1. Try to fetch resolved URL and OIDC token
    try:
        from app.scripts.find_hubs import _resolve_backend_url, _get_oidc_token
        backend_url = await _resolve_backend_url()
        oidc_token = await _get_oidc_token(backend_url)

        headers = {}
        if oidc_token:
            headers["Authorization"] = f"Bearer {oidc_token}"
        if user_id:
            headers["X-Platform-User-Id"] = user_id

        # 2. Call the search API to resolve the name if it is not a direct ID
        # Hub IDs are typically UUIDs (length >= 28). If it is shorter, it's likely a name query.
        is_id = len(hubId) >= 28 and "-" in hubId
        
        if not is_id:
            search_endpoint = f"{backend_url}/api/discovery/search"
            params = {"query": hubId}
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(search_endpoint, params=params, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    hubs = resp.json()
                    if hubs:
                        # Exact or close matches using name list
                        names = [h.get("name", "") for h in hubs]
                        matches = difflib.get_close_matches(hubId, names, n=1, cutoff=0.5)
                        if matches:
                            matched_name = matches[0]
                            # Find the hub ID corresponding to matched_name
                            for h in hubs:
                                if h.get("name") == matched_name:
                                    resolved_id = h.get("id")
                                    logger.info(f"[find-hub] Resolved fuzzy query '{hubId}' to ID '{resolved_id}' ({matched_name})")
                                    break
                        else:
                            # Fallback to the first result
                            resolved_id = hubs[0].get("id")
                            logger.info(f"[find-hub] Fallback resolved query '{hubId}' to ID '{resolved_id}'")
    except Exception as e:
        logger.warning(f"[find-hub] Failed active name resolution for '{hubId}': {e}")

    # 3. Register the SWITCH_HUB action in context
    context.actions.append({
        "type": "SWITCH_HUB",
        "payload": {
            "hubId": resolved_id
        }
    })
    
    return {
        "status": "success", 
        "message": f"Navigating to hub: {resolved_id}",
        "resolvedHubId": resolved_id
    }
