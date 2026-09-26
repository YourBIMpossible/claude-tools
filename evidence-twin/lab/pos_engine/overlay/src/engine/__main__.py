import sys

from src.engine import dispatch

print(dispatch(sys.argv[1])(int(sys.argv[2])))
