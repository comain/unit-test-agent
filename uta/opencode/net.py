def format_host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def build_base_url(host: str, port: int) -> str:
    return f"http://{format_host_for_url(host)}:{port}"
