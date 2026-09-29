import sys


def main() -> None:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--apply-update":
        from src.update_manager import apply_update_from_args

        try:
            raise SystemExit(apply_update_from_args(arguments))
        except Exception as error:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Update failed", str(error), parent=root)
                root.destroy()
            except Exception:
                pass
            raise SystemExit(1) from error
    if arguments and arguments[0] == "--cleanup-update-helper" and len(arguments) == 2:
        from src.update_manager import cleanup_update_helper_later

        cleanup_update_helper_later(arguments[1])
    from src.app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
