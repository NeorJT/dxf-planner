"""
Utilidades de color para conversión de AutoCAD ACI a valores HEX.
"""

ACI_COLORS = {
    1: "#FF0000", 2: "#FFFF00", 3: "#00FF00", 4: "#00FFFF",
    5: "#0000FF", 6: "#FF00FF", 7: "#FFFFFF", 8: "#808080",
    9: "#C0C0C0", 30: "#FF8000", 40: "#FFBF00", 50: "#FFFF40",
    70: "#00FF80", 90: "#00FFFF", 130: "#0080FF", 170: "#8000FF",
    200: "#FF0080", 250: "#A0A0A0", 251: "#808080", 252: "#606060",
    253: "#404040", 254: "#202020", 255: "#000000",
}

def aci_to_hex(aci: int) -> str:
    return ACI_COLORS.get(aci, "#CCCCCC")
