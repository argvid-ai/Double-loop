"""Record authenticated control ingress without recording credentials or pretending identity."""
import asyncio
import logging


async def record(host, task_id, kind, facts):
    try:
        await asyncio.to_thread(host.store.audit, task_id, kind, facts)
        return True
    except Exception as exc:
        # Logging failure must not veto an authenticated safety stop.
        logging.getLogger(__name__).warning(
            "Control provenance save failed (%s); actor attribution unavailable", type(exc).__name__)
        return False


async def receive_control(host, body, request):
    # Capture transport provenance, not caller-provided headers claiming to be an owner.
    args = body.arguments()
    control = body.control
    task_id = getattr(args, "task_id", None)
    base = {
        "request_id": body.request_id, "principal": body.principal,
        "operation": body.operation, "task_id": task_id,
        "action": getattr(control, "action", None),
        "version": getattr(control, "version", None),
        "generation": getattr(control, "generation", None),
        "transport": "authenticated_node_http",
        "client_host": request.client.host if request.client else None,
        "human_actor": None, "human_authorization_verified": False,
        "meaning": "节点凭据已认证，但共享凭据本身不证明具体操作者；需用request_id关联发送端原始事件。",
    }
    received = await record(host, task_id, "observation.control.received", base)
    try:
        result = await host.observations.receive(body)
    except Exception as exc:
        await record(host, task_id, "observation.control.rejected",
                     {**base, "error_type": type(exc).__name__})
        raise
    accepted = await record(host, task_id, "observation.control.accepted",
                            {**base, "acceptance_only": True})
    return {**result, "control_trace_recorded": received and accepted}
