"""Sample ``/tracing/details`` payloads used across the test suite.

``DELIVERED`` is the real structure of a delivered NL parcel, contributed by
@joerimul in issue #1 (personal values were already redacted by the
contributor and are replaced with obvious placeholders here). Trimmed to the
fields the integration reads, plus the blocks diagnostics must redact.

``IN_TRANSIT`` is the same shape with a **deliberately unmapped** state and
only the wide ``low``/``high`` window populated — the situation before the
tour is planned. It exercises the "unknown status" path and the delivery-window
fallback. The state name is invented on purpose: no non-delivered state has
been observed, so no test may claim one is real.
"""

DELIVERED = {
    "trunkrsNr": "419719666",
    "shipmentId": "19713692",
    "merchantName": "ExampleShop",
    "senderName": "ExampleShop",
    "merchantProfileName": "QLS - Next Day",
    "recipientName": "John Doe",
    "product": "SAME_DAY",
    "region": "WBR",
    "remark": None,
    "leaveBehindRemark": "In het paleis",
    "shipmentFeatures": {
        "requirePOD": False,
        "noSignature": False,
        "noNeighbourDelivery": False,
        "deliverInMailbox": False,
        "leaveBehindPermission": True,
    },
    "currentState": {
        "stateName": "SHIPMENT_DELIVERED",
        "setAt": "2026-07-10T17:46:17.864Z",
        "reasonCode": None,
        "neighbourAddressLine": None,
        "neighbourName": None,
    },
    "deliveryAttempts": [
        {
            "stateName": "SHIPMENT_DELIVERED",
            "setAt": "2026-07-10T17:46:17.864Z",
            "reasonCode": None,
        }
    ],
    "recipientLocation": {
        "address": "Straat Huisnummer",
        "postal_code": "0000AA",
        "postalCode": "0000AA",
        "city": "Duckstad",
        "country": "NL",
        "latitude": 0.0,
        "longitude": 0.0,
        "recipient_id": 38823951,
        "version": 0,
    },
    "timeSlot": {
        "low": "2026-07-10T15:00:00.000Z",
        "high": "2026-07-10T20:30:00.000Z",
        "from": "2026-07-10T17:34:40.318Z",
        "to": "2026-07-10T18:00:55.318Z",
    },
    "auditLogs": [
        {
            "shipmentId": "19713692",
            "createdAt": "2026-07-10T17:46:17.877Z",
            "userSub": "6fb53d2c-0000-0000-0000-000000000000",
            "source": "[DRIVER-APP]: Shipment delivered",
        }
    ],
    "tourDetails": {
        "tourId": 2616692,
        "tourDate": "2026-07-10T00:00:00.000Z",
        "driverId": 13771,
        "position": 17,
        "eta": "2026-07-10T17:46:51.568Z",
        "totalDelay": 801,
        "networkType": "NIGHT",
        "polyline": "ume{Hej|ZfAdADSVkABE",
    },
    "merchantAllowsAddressChange": True,
    "merchantConfiguration": {
        "isAddressChangeAllowed": True,
        "isLeaveBehindAllowed": True,
    },
    "theme": None,
}

IN_TRANSIT = {
    **DELIVERED,
    "trunkrsNr": "419719667",
    "currentState": {
        # Invented: no non-delivered state has been observed yet.
        "stateName": "SHIPMENT_SOME_UNMAPPED_STATE",
        "setAt": "2026-07-10T11:07:46.198Z",
        "reasonCode": None,
    },
    "deliveryAttempts": [],
    # Before the tour is planned only the wide promised slot is filled in.
    "timeSlot": {
        "low": "2026-07-10T15:00:00.000Z",
        "high": "2026-07-10T20:30:00.000Z",
        "from": None,
        "to": None,
    },
}
