"""Container entry point: fail closed if web credentials are incomplete."""
import os


def main():
    if not all(os.environ.get(k) for k in ("PIM_AUTH_USER", "PIM_AUTH_PASS")):
        raise SystemExit("必须设置 PIM_AUTH_USER 和 PIM_AUTH_PASS。")
    os.environ["PIM_ALLOW_RTD_REFRESH"] = "0"
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, workers=1)


if __name__ == "__main__":
    main()
