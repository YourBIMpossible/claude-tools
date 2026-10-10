"""Transform engine: a name is dispatched to one of twelve kernels."""

import importlib

KERNELS = [f"k_{c}" for c in "abcdefghijkl"]


def dispatch(name: str):
    kernel = KERNELS[sum(map(ord, name)) % len(KERNELS)]
    return importlib.import_module(f"src.engine.{kernel}").apply
