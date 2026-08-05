/**
 * The last line before a white screen.
 *
 * React unmounts the entire tree when a render throws and nothing catches it.
 * There was no boundary anywhere in this app, so a single bad value on a
 * single screen took the whole thing down to a blank page — no header, no
 * navigation, no message, and no way back except reloading by hand. The one
 * that actually did it was `?tab=` on Master data carrying a value that is not
 * a tab: the lookup returned undefined and the next property read threw.
 *
 * That specific bug is fixed at its source. This exists because the next one
 * has not been found yet, and the cost of a screen that fails badly should be
 * that screen, not the session.
 *
 * A class component on purpose — `componentDidCatch` has no hook equivalent.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { Button } from "./ui";

interface Props {
  children: ReactNode;
  /**
   * Changing this resets the boundary.
   *
   * Routed boundaries pass the pathname, so navigating away from a screen that
   * threw gives you a working app again. Without it the boundary stays latched
   * and every subsequent route renders the same error page.
   */
  resetKey?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // There is no error-reporting service wired up, and inventing one here
    // would be a dependency this project does not have. The console is what a
    // developer will actually look at, and the component stack is the half
    // that says *where*, which the message alone does not.
    console.error("Unhandled render error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center px-6 text-center">
        <div className="mb-3 rounded-full bg-danger-soft p-3">
          <TriangleAlert className="size-5 text-danger" />
        </div>
        <p className="text-sm font-medium text-ink">This screen stopped working</p>
        <p className="mt-1 max-w-sm text-[13px] text-ink-soft">
          Nothing you were looking at has been changed. Move to another screen,
          or reload to start again.
        </p>
        <p className="mt-3 max-w-lg font-mono text-[11px] break-words text-ink-faint">
          {this.state.error.message}
        </p>
        <div className="mt-4 flex gap-2">
          <Button onClick={() => this.setState({ error: null })}>Try again</Button>
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      </div>
    );
  }
}
