"""Decode SNMP trap/inform v1/v2c memakai skema pysnmp + pyasn1.

Kami memakai pysnmp untuk mendefinisikan struktur PDU dan pyasn1 untuk
BER decode/encode. Ini menghindari event loop asyncio pysnmp 7 yang tidak
cocok dengan thread penerima sederhana. v3 USM belum diimplementasikan
(dicatat di docs/TEMUAN.md).
"""

import json

from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.codec.ber import encoder as ber_encoder
from pysnmp.proto.api import v1, v2c

TRAP_OID_VARBIND = "1.3.6.1.6.3.1.1.4.1.0"
IF_INDEX_OID = "1.3.6.1.2.1.2.2.1.1"

STANDARD_TRAPS = {
    "1.3.6.1.6.3.1.1.5.1": "coldStart",
    "1.3.6.1.6.3.1.1.5.2": "warmStart",
    "1.3.6.1.6.3.1.1.5.3": "linkDown",
    "1.3.6.1.6.3.1.1.5.4": "linkUp",
    "1.3.6.1.6.3.1.1.5.5": "authenticationFailure",
    "1.3.6.1.6.3.1.1.5.6": "egpNeighborLoss",
    "1.3.6.1.6.3.1.1.5.7": "enterpriseSpecific",
}
_V1_GENERIC = {
    0: "1.3.6.1.6.3.1.1.5.1",
    1: "1.3.6.1.6.3.1.1.5.2",
    2: "1.3.6.1.6.3.1.1.5.3",
    3: "1.3.6.1.6.3.1.1.5.4",
    4: "1.3.6.1.6.3.1.1.5.5",
    5: "1.3.6.1.6.3.1.1.5.6",
    6: "1.3.6.1.6.3.1.1.5.7",
}


class TrapDecodeError(ValueError):
    pass


def _value_text(value):
    try:
        text = value.prettyPrint()
    except Exception:
        text = str(value)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return str(text)[:512]


def _varbind_list(pdu, v2=True):
    result = []
    api = v2c.apiPDU if v2 else v1.apiTrapPDU
    for oid, value in api.get_varbinds(pdu):
        result.append({"oid": str(oid), "type": value.__class__.__name__,
                       "value": _value_text(value)})
    return result


def decode_trap(data):
    """Return dict trap atau raise TrapDecodeError."""
    if not data:
        raise TrapDecodeError("paket kosong")
    message = None
    version = None
    try:
        message, rest = ber_decoder.decode(bytes(data), asn1Spec=v2c.Message())
        version = int(message["version"])
        if version not in (0, 1) or rest:
            message = None
    except Exception:
        message = None
    if message is None:
        try:
            message, rest = ber_decoder.decode(bytes(data), asn1Spec=v1.Message())
            version = int(message["version"])
            if version != 0 or rest:
                raise TrapDecodeError("versi SNMP tidak dikenal")
        except Exception as exc:
            raise TrapDecodeError(f"BER tidak valid: {exc}") from exc

    community = bytes(message["community"]).decode("utf-8", "replace")
    # v1 dan v2c sama-sama memakai CHOICE PDUs; ambil alternatif yang terpilih.
    pdu = message["data"].getComponent()
    if version == 0:
        pdu_type = pdu.__class__.__name__
        generic = int(pdu["generic-trap"])
        specific = int(pdu["specific-trap"])
        enterprise = str(pdu["enterprise"])
        trap_oid = _V1_GENERIC.get(generic)
        if generic == 6 and enterprise:
            trap_oid = f"{enterprise}.0.{specific}"
        if not trap_oid:
            trap_oid = "1.3.6.1.6.3.1.1.5.7"
        varbinds = _varbind_list(pdu, v2=False)
        request_id = None
    else:
        pdu_type = pdu.__class__.__name__
        varbinds = _varbind_list(pdu, v2=True)
        trap_oid = None
        for item in varbinds:
            if item["oid"] == TRAP_OID_VARBIND:
                trap_oid = item["value"]
                break
        if not trap_oid:
            raise TrapDecodeError("snmpTrapOID.0 tidak ada")
        request_id = int(v2c.apiPDU.get_request_id(pdu))

    if_index = None
    for item in varbinds:
        if item["oid"] == IF_INDEX_OID:
            try:
                if_index = int(item["value"])
            except (TypeError, ValueError):
                pass
            break

    return {
        "version": "v1" if version == 0 else "v2c",
        "community": community,
        "pdu_type": pdu_type,
        "request_id": request_id,
        "trap_oid": str(trap_oid),
        "trap_name": STANDARD_TRAPS.get(str(trap_oid)),
        "varbinds": varbinds,
        "if_index": if_index,
        "is_inform": pdu_type == "InformRequestPDU",
        "_pdu": pdu,
    }


def varbinds_json(varbinds):
    return json.dumps(varbinds, ensure_ascii=False)[:16384]


def build_inform_response(decoded):
    """Bangun Response PDU untuk SNMPv2c inform (agar pengirim tidak retry)."""
    if not decoded.get("is_inform") or decoded.get("version") != "v2c":
        return None
    request = decoded["_pdu"]
    response = v2c.ResponsePDU()
    v2c.apiPDU.set_defaults(response)
    v2c.apiPDU.set_request_id(response, v2c.apiPDU.get_request_id(request))
    v2c.apiPDU.set_varbinds(response, v2c.apiPDU.get_varbinds(request))
    message = v2c.Message()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, decoded.get("community") or "")
    v2c.apiMessage.set_pdu(message, response)
    return ber_encoder.encode(message)
