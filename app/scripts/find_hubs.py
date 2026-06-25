import logging
import asyncio
import hubscape_adk

logger = logging.getLogger(__name__)

async def find_hubs(query: str) -> dict:
    """Finds and lists discoverable public and private hubs on the platform that match the search query.

    Args:
        query: The search term or name of the hub to find.
    """
    context = hubscape_adk.get_context()
    user_id = context.auth.user_id
    logger.info(f"[find-hub] Executing find_hubs search: query='{query}', user_id='{user_id}'")

    db_client = context._db_client
    
    try:
        # Gather candidates
        candidates = {}

        # 1. Fetch public hubs
        # To avoid index errors and Firestore limit issues, fetch enabled hubs in thread
        public_query = db_client.collection_group("hubs").where("discovery.enabled", "==", True).limit(100)
        public_docs = await asyncio.to_thread(public_query.stream)
        for doc in public_docs:
            h_data = doc.to_dict()
            h_data["id"] = doc.id
            path_parts = doc.reference.path.split('/')
            if len(path_parts) >= 2:
                h_data["orgId"] = path_parts[1]
            candidates[doc.id] = h_data

        # 2. Fetch member private hubs for the current user
        if user_id and user_id != "anonymous_user":
            member_query = db_client.collection_group("members").where("id", "==", user_id)
            member_docs = await asyncio.to_thread(member_query.stream)
            for mem_doc in member_docs:
                hub_ref = mem_doc.reference.parent.parent
                if not hub_ref:
                    continue
                hub_path = hub_ref.path
                hub_parts = hub_path.split('/')
                if len(hub_parts) == 4 and hub_parts[2] == 'hubs':
                    if hub_ref.id not in candidates:
                        h_snap = await asyncio.to_thread(hub_ref.get)
                        if h_snap.exists:
                            h_data = h_snap.to_dict()
                            h_data["id"] = hub_ref.id
                            h_data["orgId"] = hub_parts[1]
                            candidates[hub_ref.id] = h_data

        # 3. Filter by Active/Non-deleted Org Status
        unique_org_ids = {h.get('orgId') for h in candidates.values() if h.get('orgId')}
        active_org_ids = set()
        if unique_org_ids:
            org_refs = [db_client.collection("organizations").document(oid) for oid in unique_org_ids]
            org_snaps = await asyncio.to_thread(db_client.get_all, org_refs)
            for snap in org_snaps:
                if snap.exists:
                    o_data = snap.to_dict() or {}
                    if not o_data.get('isDeleted', False) and o_data.get('isActive', True):
                        active_org_ids.add(snap.id)

        filtered_candidates = {}
        for hid, hdata in candidates.items():
            oid = hdata.get('orgId')
            if not oid or oid in active_org_ids:
                filtered_candidates[hid] = hdata

        candidates = filtered_candidates

        # 4. Tokenize query and score candidates
        # Tokenize (Stop words filtering)
        stop_words = {
            'the', 'and', 'or', 'at', 'in', 'on', 'to', 'for', 'is', 'a', 'of', 'with', 
            'are', 'what', 'where', 'when', 'who', 'how', 'why', 'does', 'do', 'can', 'be',
            'hub', 'hubs'
        }
        clean_query = "".join([c if c.isalnum() else " " for c in query.lower()])
        q_terms = [t for t in clean_query.split() if len(t) > 1 and t not in stop_words]

        scored_results = []
        for h in candidates.values():
            if not h.get('isActive', True) or h.get('isDeleted', False):
                continue
            
            # calculate relevance
            score = 0
            name = (h.get('name') or '').lower()
            desc = (h.get('description') or '').lower()
            discovery = h.get('discovery') or {}
            tags = [t.lower() for t in (discovery.get('tags') or [])]
            location = (discovery.get('location') or {}).get('location', {}).get('label', '').lower()

            for term in q_terms:
                if term in name:
                    score += 10 # SCORE_NAME_MATCH
                elif any(term in t for t in tags):
                    score += 5 # SCORE_TAG_MATCH
                elif term in desc:
                    score += 3 # SCORE_DESC_MATCH
                elif term in location:
                    score += 1 # SCORE_LOCATION_MATCH
            
            if score > 0 or not q_terms: # if no search terms, include it with score 1 or 0
                h['_score'] = score
                scored_results.append(h)

        if not scored_results:
            return {"status": "success", "result": "No matching hubs found."}

        scored_results.sort(key=lambda x: x.get('_score', 0), reverse=True)

        # Apply 50% threshold if query contains terms
        if q_terms:
            max_score = scored_results[0]['_score']
            threshold = (max_score / 2) if max_score > 5 else 1
            final_hubs = [h for h in scored_results if h['_score'] >= threshold]
        else:
            final_hubs = scored_results

        # Format output
        results = []
        for h in final_hubs[:5]:
            discovery_meta = h.get('discovery') or {}
            results.append({
                "id": h['id'],
                "name": h.get('name') or 'Unknown',
                "description": h.get('description') or '',
                "location": (discovery_meta.get('location') or {}).get('label', 'Unknown')
            })

        formatted_result = ""
        for idx, r in enumerate(results):
            formatted_result += f"--- Hub {idx+1}: {r['name']} (ID: {r['id']}) ---\n"
            if r['description']:
                formatted_result += f"Description: {r['description']}\n"
            if r['location']:
                formatted_result += f"Location: {r['location']}\n"
            formatted_result += "\n"

        return {"status": "success", "result": formatted_result.strip()}
    except Exception as e:
        logger.error(f"[find-hub] find_hubs failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
