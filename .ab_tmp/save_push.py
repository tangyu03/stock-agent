import re
import requests

urls = [
    "https://www.pushplus.plus/shortMessage/9a2a5b825bd94f11a35e80ef629eb6d5",
    "https://www.pushplus.plus/shortMessage/eb11aa86d221489fb867850568b3aca4",
]
for index, url in enumerate(urls, 1):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    text = response.text
    match = re.search(
        r'<div class="container-fluid px-0 list-unstyled" id="messageContent"[^>]*>(.*?)</div>',
        text,
        re.S,
    )
    content = match.group(1) if match else text
    with open(f".ab_tmp/push_{index}.html", "w", encoding="utf-8") as f:
        f.write(content)
    print(url, len(content))
