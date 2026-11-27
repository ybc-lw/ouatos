"""Helpers to turn a virtual Screen into a PNG."""
from PIL import Image
def save_png(scr, path):
    Image.frombytes('RGB', (scr.w, scr.h), scr.to_ppm()).save(path)
