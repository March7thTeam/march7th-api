"""Gateway error responses with explicit legacy/OpenAI wire contracts."""
import secrets
from fastapi.responses import JSONResponse


def json_error(status_code, error, message, *, request_id=None, param=None, code=None, openai=False):
    if not openai:
        return JSONResponse(status_code=status_code, content={"error": error, "message": message},
                            headers={"X-Request-ID": request_id} if request_id else None)
    request_id = request_id or secrets.token_hex(16)
    detail = {"message": message, "type": error}
    if param is not None:
        detail["param"] = param
    if code is not None:
        detail["code"] = code
    return JSONResponse(status_code=status_code, content={"error": detail}, headers={"X-Request-ID": request_id})
