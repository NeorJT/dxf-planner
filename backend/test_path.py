import requests
import json

BASE_URL = "http://localhost:8000/api"

def main():
    print("Loading plano_filtrado_bueno.dxf...")
    r = requests.post(f"{BASE_URL}/dxf/load-local?filename=plano_filtrado_bueno.dxf")
    if r.status_code != 200:
        print(f"Error loading plan: {r.status_code} - {r.text}")
        return
    
    content = r.content
    meta_len = int.from_bytes(content[0:4], "little")
    meta_json = json.loads(content[4:4+meta_len].decode("utf-8"))
    plan_id = meta_json["plan_id"]
    
    # We will pick two coordinates close to the center
    min_x, max_x = meta_json["bbox"]["minX"], meta_json["bbox"]["maxX"]
    min_y, max_y = meta_json["bbox"]["minY"], meta_json["bbox"]["maxY"]
    
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    
    print(f"Center coords: ({cx}, {cy})")
    
    # Let's request a path between two points near the center
    # Waypoints: wp1 = (cx, cy), wp2 = (cx + 1000, cy + 1000)
    req_body = {
        "plan_id": plan_id,
        "waypoints": [
            {"x": cx, "y": cy},
            {"x": cx + 2000, "y": cy + 2000}
        ],
        "clearance": 0.5
    }
    
    print("Requesting path...")
    r_path = requests.post(f"{BASE_URL}/pathfinding/find-path-waypoints", json=req_body)
    print("Response status:", r_path.status_code)
    if r_path.status_code == 200:
        res = r_path.json()
        print("Path distance:", res["distance"])
        print("Number of points:", res["points"])
    else:
        print("Error:", r_path.text)

if __name__ == "__main__":
    main()
