import json
for p in json.load(open("gumroad_products.json")): print(p["name"],"|",p["id"],"|",p["url"])
