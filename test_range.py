import requests

URL = "https://tunnel1.dlproxy.uk/download/s0Jb9lsw8n53AYLRv03-3Y2G2T8boElXjI39gp-Rsryfz92hm_6brG04B7-j8394FiBvelEzW2rMr4RC2zHOo_AAhHtyixEdzoBo7wi-EGQbJyJUFBO4UnAdYHgDwDrbdK2MeLhE0UDt-6pBayLYjd9A8xKf_xiABeRbXcEoNpZcldGyVcC9VHptKw-584u2z1Wycd2z45LGLBzha0R0VYnSE0zgL0X4PCJdOrhXAx04gw6m7c55tONx5McboPwozoHkhOYHeAPhktnuTX_nEv13fLQXt5LSJbNOf3P7UfdmbYkYLsQ-QencR0pm8rygp4xMpilQfjKCVHjViF-cyvj_msiBiZ82UOaaaJZ39e8lQ7PkXCfLohoaFEPvf1kn9eWBrBxEBX12AEEwAZa6ZEMB0Rg0_EpeA8HJc3t8607lFyn0oiHUPQo_bYQB2X_DC0hlbvhBCKtbR9Luv_8xTXO_70Bcxfx5QfL3mo-a427cLwC_XY-gMHmcAMApRTdzhKQBOnMN2OGj1he6Ml3clPh7HJT2dEh8w8MGf5OJ5HQh7df3OstswZ355UckuaG5y8HRlG51DYKOBe3z8DLxEw?sig=qyIpH6-EDS_NLyfv_hJdwY7tPUqvNpoSs_oSwq35OUk"

print("Testing:", URL)

try:
    r = requests.get(
        URL,
        headers={"Range": "bytes=0-999999"},
        stream=True,
        allow_redirects=True,
        timeout=30,
    )

    print("\nFinal URL:")
    print(r.url)

    print("\nHTTP status:")
    print(r.status_code)

    print("\nContent-Type:")
    print(r.headers.get("Content-Type"))

    print("\nContent-Length:")
    print(r.headers.get("Content-Length"))

    print("\nContent-Range:")
    print(r.headers.get("Content-Range"))

    print("\nAccept-Ranges:")
    print(r.headers.get("Accept-Ranges"))

    print("\nContent-Disposition:")
    print(r.headers.get("Content-Disposition"))

    r.close()

except Exception as e:
    print("\nERROR:")
    print(repr(e))