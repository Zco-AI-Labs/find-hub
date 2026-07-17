import os
import uuid
import importlib.util
import urllib.request
import time
from google.genai import types
from google.adk.runners import Runner
from app.core import hubscape_adk

class GEAPAgentWrapper:
    def __init__(self, agent, app_name: str = None):
        self.agent = agent
        self.app_name = app_name or agent.name.replace('_', '-')
        self.runner = None

    async def query(self, question: str, context: dict = None) -> str:
        start_time = time.time()
        core_dir = os.path.dirname(os.path.abspath(__file__))
        runtime_dir = os.path.abspath(os.path.join(core_dir, ".."))
        

        user_id = (context or {}).get("userId") or (context or {}).get("user_id") or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/Zco-AI-Labs/{self.app_name}"))
        from app.app_utils.env_resolver import get_project_id
        project_id = get_project_id()
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id, 
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=context
        )
        
        session_id = (context or {}).get("sessionId") or f"session_{user_id}_{hub_id}"
        
        # --- OPENTELEMETRY CONTEXT ENRICHMENT (OPTION A) ---
        try:
            from opentelemetry import trace
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("org_id", org_id or "unknown")
                current_span.set_attribute("hub_id", hub_id or "unknown")
                current_span.set_attribute("user_id", user_id or "unknown")
                current_span.set_attribute("gen_ai.conversation_id", session_id)
                current_span.set_attribute("gen_ai.request.model", self.agent.model.model_name)
                current_span.set_attribute("provider", "vertex")
                
                # Determine query type (direct vs nested A2A) using call depth
                depth = (context or {}).get("depth", 0)
                request_type = "a2a" if depth > 0 else "direct"
                current_span.set_attribute("gen_ai.request.type", request_type)
        except Exception as otel_err:
            print(f"⚠️ Failed to set OpenTelemetry span attributes: {otel_err}")
        # ----------------------------------------------------
        
        with hubscape_adk.context_session(remote_ctx):
            if not self.runner:
                from google.adk.sessions.in_memory_session_service import InMemorySessionService
                from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
                from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
                from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
                
                self.runner = Runner(
                    agent=self.agent,
                    app_name=self.app_name,
                    session_service=InMemorySessionService(),
                    artifact_service=InMemoryArtifactService(),
                    memory_service=InMemoryMemoryService(),
                    credential_service=InMemoryCredentialService(),
                    auto_create_session=True
                )
            
            new_message = types.Content(
                parts=[types.Part.from_text(text=question)]
            )
            
            text_response = ""
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message
            ):
                if event.output:
                    text_response += event.output
                elif event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            text_response += part.text
            
            # Record final execution latency on active span
            try:
                from opentelemetry import trace
                current_span = trace.get_current_span()
                if current_span:
                    latency_ms = (time.time() - start_time) * 1000.0
                    current_span.set_attribute("latency_ms", float(latency_ms))
            except Exception as otel_err:
                pass
                
            return text_response
