import requests
from requests.adapters import HTTPAdapter


class SourceAddressAdapter(HTTPAdapter):
    def __init__(self, source_ip, **kwargs):
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        return super().proxy_manager_for(*args, **kwargs)


def test_interface(name, source_ip):
    print(f"\nTesting {name}")
    print(f"Local IP: {source_ip}")

    session = requests.Session()

    adapter = SourceAddressAdapter(source_ip)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.get(
            "https://api.ipify.org?format=json",
            timeout=15
        )

        response.raise_for_status()

        print("Public IP:", response.json()["ip"])

    except Exception as e:
        print("FAILED:")
        print(repr(e))


wifi_ip = "192.168.0.131"
usb_ip = "10.164.141.46"

test_interface("Ethernet", wifi_ip)
test_interface("Ethernet 3", usb_ip)