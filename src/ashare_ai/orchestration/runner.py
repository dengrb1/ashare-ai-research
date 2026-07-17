from __future__ import annotations


def main() -> None:
    from ashare_ai.orchestration.daily import scheduled_daily_research_flow

    if not hasattr(scheduled_daily_research_flow, "serve"):
        raise RuntimeError("Install the orchestration extra: pip install '.[orchestration]'")
    scheduled_daily_research_flow.serve(name="after-close-daily", cron="0 18 * * 1-5")


if __name__ == "__main__":
    main()
