import sys
# Prevent shadowing of 'py' library by stray py.py in Python root
sys.path = [p for p in sys.path if p.rstrip('\\/').lower() != r'c:\users\bharath\appdata\local\programs\python\python312']
import pytest

if __name__ == '__main__':
    args = sys.argv[1:] if len(sys.argv) > 1 else ['tests']
    sys.exit(pytest.main(args))
