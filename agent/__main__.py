"""允许通过 python -m agent 启动 (默认 TUI 模式，--classic 回退到传统 CLI)"""
import sys

if __name__ == "__main__":
    if "--classic" in sys.argv:
        sys.argv.remove("--classic")
        from agent.cli import main
    else:
        from agent.tui import main
    main()
