"""Validasi daftar jaringan CIDR yang diizinkan untuk pemantauan."""
import ipaddress


def parse_allowed_networks(raw):
    """
    Parse CIDR list dipisahkan koma. Raise ValueError bila ada CIDR invalid.
    """
    networks = []
    if not raw:
        return networks
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError as exc:
            raise ValueError(f"ALLOWED_NETWORKS berisi CIDR tidak valid: {chunk!r}") from exc
    return networks
