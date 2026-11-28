async def app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})
