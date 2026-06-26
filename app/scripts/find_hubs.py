import logging
import asyncio
import os
import httpx
import google.auth
import google.auth.transport.requests
import hubscape_adk
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

async def _resolve_backend_url() -> str:
    """Resolves the backend Cloud Run service URL dynamically."""
    # 1. Check environment variables first
    url = os.getenv("HUBSCAPE_API_URL") or os.getenv("BACKEND_URL") or os.getenv("VITE_API_URL")
    if url:
        return url.rstrip("/")

    # 2. Try to fetch from GCP Cloud Run Admin API
    try:
        from app.app_utils.env_resolver import get_project_id, get_region
        project_id = get_project_id()
        region = get_region()
        
        # Get access token from metadata server or default credentials
        token = None
        try:
            import httpx as httpx_sync
            meta_url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
            resp = httpx_sync.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=1.0)
            if resp.status_code == 200:
                token = resp.json().get("access_token")
        except Exception:
            pass

        if not token:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        run_api_url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_id}/services/hubscape-backend"
        async with httpx.AsyncClient() as client:
            resp = await client.get(run_api_url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", {})
                url_resolved = status.get("url")
                if url_resolved:
                    return url_resolved.rstrip("/")
    except Exception as e:
        logger.warning(f"[find-hub] Failed to resolve Cloud Run backend URL dynamically: {e}")

    # 3. Local default fallback
    return "http://localhost:8000"

async def _get_oidc_token(audience: str) -> Optional[str]:
    """Generates a GCP OIDC ID Token for the target backend service audience."""
    # 1. Try Metadata Server (inside Cloud Run / Vertex AI Reasoning Engine sandbox)
    try:
        import httpx as httpx_sync
        meta_url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}"
        resp = httpx_sync.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=2.0)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception:
        pass

    # 2. Try local user credentials fallback (for local development/testing)
    try:
        import google.oauth2.id_token
        import google.auth.transport.requests
        auth_req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        return token
    except Exception:
        pass

    return None

async def find_hubs(query: str) -> dict:
    """Finds and lists discoverable public and private hubs on the platform that match the search query.

    Args:
        query: The search term or name of the hub to find.
    """
    context = hubscape_adk.get_context()
    user_id = context.auth.user_id
    logger.info(f"[find-hub] Executing find_hubs search: query='{query}', user_id='{user_id}'")

    try:
        backend_url = await _resolve_backend_url()
        logger.info(f"[find-hub] Resolved backend endpoint: {backend_url}")

        # Fetch OIDC identity token for service-to-service auth
        oidc_token = await _get_oidc_token(backend_url)

        headers = {}
        if oidc_token:
            headers["Authorization"] = f"Bearer {oidc_token}"
        if user_id:
            headers["X-Platform-User-Id"] = user_id

        # Make the request to the platform backend
        search_endpoint = f"{backend_url}/api/discovery/search"
        params = {"query": query}

        async with httpx.AsyncClient() as client:
            resp = await client.get(search_endpoint, params=params, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"[find-hub] Backend search failed: {resp.status_code} - {resp.text}")
                return {"status": "error", "message": f"Platform search failed with status {resp.status_code}"}

            hubs = resp.json()

        if not hubs:
            return {"status": "success", "result": "No matching hubs found."}

        # Format output exactly as expected by the agent
        formatted_result = ""
        for idx, h in enumerate(hubs):
            formatted_result += f"--- Hub {idx+1}: {h.get('name', 'Unknown')} (ID: {h.get('id', '')}) ---\n"
            if h.get('description'):
                formatted_result += f"Description: {h['description']}\n"
            if h.get('location'):
                formatted_result += f"Location: {h['location']}\n"
            formatted_result += "\n"

        return {"status": "success", "result": formatted_result.strip()}
    except Exception as e:
        logger.error(f"[find-hub] find_hubs failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
