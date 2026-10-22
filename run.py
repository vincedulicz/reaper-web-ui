import argparse

from app import create_app
from app.backends import create_backend
from app.logs import setup_logging

setup_logging()


def main() -> None:
    parser = argparse.ArgumentParser(description="REAPER web remote")
    parser.add_argument("--backend", choices=("reapy", "mock"), default="reapy")
    parser.add_argument("--host", default="0.0.0.0", help="use 0.0.0.0 to reach it from other devices")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"Már fut valami a {args.port}-es porton, kilépek.")
            return

    app = create_app(create_backend(args.backend))
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
