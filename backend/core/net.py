"""Validasi daftar jaringan CIDR yang diizinkan untuk pemantauan."""
import ipaddress


# Rentang yang diizinkan bila ALLOWED_NETWORKS kosong (selaras dengan
# dokumentasi .env.example): RFC1918 (IPv4, RFC 1918) dan ULA fc00::/7
# (IPv6, RFC 4193). Loopback, link-local, CGNAT (100.64.0.0/10), dan ruang
# alamat dokumentasi/publik TIDAK termasuk - hanya rentang privat yang
# boleh dipakai sebagai target monitoring.
DEFAULT_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


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


def ip_default_allowed(addr):
    """True bila addr berada di rentang privat yang diizinkan secara default."""
    return any(
        addr.version == net.version and addr in net for net in DEFAULT_ALLOWED_NETWORKS
    )


def network_default_allowed(network):
    """True bila seluruh rentang network berada di rentang privat default."""
    try:
        return any(
            network.version == net.version and network.subnet_of(net)
            for net in DEFAULT_ALLOWED_NETWORKS
        )
    except TypeError:
        return False
