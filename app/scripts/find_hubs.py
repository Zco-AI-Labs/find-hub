import logging
import asyncio
import os
import httpx
import google.auth
import google.auth.transport.requests
import hubscape_adk
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

def _validate_backend_url(url: str, project_id: str) -> str:
    """Validates the backend URL based on strict production boundaries."""
    if not url:
        raise ValueError("Backend URL cannot be empty.")
         
    # Check if we are running in the production GCP project
    if project_id == "hubscape-production":
        url_lower = url.lower()
        
        # Enforce strict whitelist in production
        production_whitelist = [
            "hubscape-backend-lvktsydgdq-uc.a.run.app"
        ]
        
        is_whitelisted = any(domain in url_lower for domain in production_whitelist)
        if not is_whitelisted:
            raise ValueError(f"Security Alert: Connection target {url} is not whitelisted for production.")
            
    return url

async def _resolve_backend_url() -> str:
    """Resolves the backend Cloud Run service URL dynamically, enforcing security boundaries."""
    from app.app_utils.env_resolver import get_project_id
    project_id = get_project_id()
    
    url = None

    # 1. Check context payload first (passed dynamically from FastAPI incoming request)
    try:
        context = hubscape_adk.get_context()
        if context and context.raw_context:
            context_url = context.raw_context.get("backend_url")
            if context_url:
                url = context_url.rstrip("/")
                logger.info(f"[find-hub] Found backend URL from context: {url}")
    except Exception as e:
        logger.debug(f"[find-hub] Could not resolve backend URL from context: {e}")

    # 2. Check environment variables (e.g. for local testing/debugging)
    if not url:
        env_url = os.getenv("HUBSCAPE_API_URL") or os.getenv("BACKEND_URL") or os.getenv("VITE_API_URL")
        if env_url:
            url = env_url.rstrip("/")
            logger.info(f"[find-hub] Found backend URL from env: {url}")

    # 3. Map known projects to their respective Cloud Run URLs to avoid IAM permission errors
    if not url:
        try:
            project_url_map = {
                "hubscape-geap": "https://hubscape-backend-w3xi4ozhca-uc.a.run.app",
                "hubscape-production": "https://hubscape-backend-lvktsydgdq-uc.a.run.app",
                "hubscape-staging": "https://hubscape-backend-staging-w3xi4ozhca-uc.a.run.app",
            }
            if project_id in project_url_map:
                url = project_url_map[project_id]
                logger.info(f"[find-hub] Found mapped URL for project '{project_id}': {url}")
        except Exception as e:
            logger.warning(f"[find-hub] Failed to resolve mapped URL: {e}")

    # 4. Try to fetch from GCP Cloud Run Admin API (Fallback)
    if not url:
        try:
            from app.app_utils.env_resolver import get_region
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
                        url = url_resolved.rstrip("/")
        except Exception as e:
            logger.warning(f"[find-hub] Failed to resolve Cloud Run backend URL dynamically: {e}")

    # 5. Local default fallback (ONLY allowed in non-production environments)
    if not url:
        if project_id == "hubscape-production":
            raise ValueError("Security Error: No valid production backend URL could be resolved.")
        url = "http://localhost:8000"

    # Enforce strict validation on the final resolved URL
    return _validate_backend_url(url, project_id)

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
