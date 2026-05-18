"""Allow `python -m docsearch [web|search] ...`."""
import sys


def main():
    args = sys.argv[1:]
    if args and args[0] == "web":
        sys.argv = [sys.argv[0]] + args[1:]
        from .web import main as run
        run()
    else:
        if args and args[0] == "search":
            sys.argv = [sys.argv[0]] + args[1:]
        from .cli import main as run
        run()


if __name__ == "__main__":
    main()
