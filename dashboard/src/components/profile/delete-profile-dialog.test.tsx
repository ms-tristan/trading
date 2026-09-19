import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { BUTTON_PRESSED_CLASSES } from '@/components/ui/button';

import { DeleteProfileDialog } from './delete-profile-dialog';

/** Props of the {@link Harness} of the dialog. */
interface HarnessProps {
  onConfirm?: () => void;
  pending?: boolean;
  error?: string | null;
}

/**
 * Renderer used by the focus tests: the dialog is opened by a real trigger, so
 * "focus returns to the element that opened it" can be observed end to end.
 */
function Harness({ onConfirm = () => {}, pending = false, error = null }: HarnessProps) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open delete dialog
      </button>
      <DeleteProfileDialog
        open={open}
        profileId="alpha"
        pending={pending}
        error={error}
        onConfirm={onConfirm}
        onCancel={() => setOpen(false)}
      />
    </div>
  );
}

/** Render an open dialog whose handlers are spies. */
function renderOpenDialog(
  props: Partial<{ pending: boolean; error: string | null; errorDetail: string | null }> = {},
): { onConfirm: ReturnType<typeof vi.fn>; onCancel: ReturnType<typeof vi.fn> } {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <DeleteProfileDialog
      open
      profileId="alpha"
      pending={props.pending ?? false}
      error={props.error ?? null}
      errorDetail={props.errorDetail ?? null}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  );
  return { onConfirm, onCancel };
}

describe('DeleteProfileDialog', () => {
  it('renders nothing at all while it is closed', () => {
    const { container } = render(
      <DeleteProfileDialog
        open={false}
        profileId="alpha"
        pending={false}
        error={null}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('is a modal dialog labelled by the profile and described without a euphemism', () => {
    renderOpenDialog();

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Delete profile alpha?');
    expect(dialog).toHaveAccessibleDescription(
      'Every open order will be cancelled and the open position will be closed at market before the profile is removed. This cannot be undone.',
    );
    // The consequence is stated plainly: orders closed, position flattened.
    expect(dialog).toHaveTextContent('Every open order will be cancelled');
    expect(dialog).toHaveTextContent('the open position will be closed at market');
    expect(dialog).toHaveTextContent('This cannot be undone.');
  });

  it('moves the focus into the dialog on open and gives it back to the trigger on close', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const trigger = screen.getByRole('button', { name: 'Open delete dialog' });
    await user.click(trigger);

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('cancels on Escape and never confirms', async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = renderOpenDialog();

    await user.keyboard('{Escape}');

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('cancels on a click on the overlay, but never on a click inside the dialog', async () => {
    const user = userEvent.setup();
    const { onConfirm, onCancel } = renderOpenDialog();

    const dialog = screen.getByRole('dialog');
    const overlay = dialog.parentElement;
    expect(overlay).not.toBeNull();

    await user.click(screen.getByText(/This cannot be undone\./));
    expect(onCancel).not.toHaveBeenCalled();

    await user.click(overlay as HTMLElement);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('offers a destructive confirmation that fires exactly once', async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderOpenDialog();

    const confirm = screen.getByRole('button', { name: 'Delete profile' });
    expect(confirm).toHaveClass('bg-destructive');
    expect(confirm).toBeEnabled();

    await user.click(confirm);

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('disables and announces the confirmation while the delete is in flight', async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderOpenDialog({ pending: true });

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-busy', 'true');

    const confirm = screen.getByRole('button', { name: 'Delete profile' });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAttribute('aria-busy', 'true');

    await user.click(confirm);

    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('renders the failure of a refused delete inside the dialog', () => {
    renderOpenDialog({ error: 'read-only mode: mutations are disabled' });

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('read-only mode: mutations are disabled')).toBeInTheDocument();
  });

  it('keeps the raw cause of the failure as a detail line under the headline', () => {
    renderOpenDialog({
      error: 'The monitoring API refused the request',
      errorDetail: 'HTTP 403 · read-only mode: mutations are disabled',
    });

    const dialog = screen.getByRole('dialog');
    // The headline is what the operator reads first...
    expect(within(dialog).getByText('The monitoring API refused the request')).toBeInTheDocument();
    // ...and the raw status plus the server text stay visible just below it.
    expect(within(dialog).getByTestId('error-banner-detail')).toHaveTextContent(
      'HTTP 403 · read-only mode: mutations are disabled',
    );
  });

  it('paints a pressed state on the cancel and on the destructive confirmation', () => {
    renderOpenDialog();

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const confirm = screen.getByRole('button', { name: 'Delete profile' });

    expect(cancel).toHaveClass(BUTTON_PRESSED_CLASSES.ghost);
    expect(confirm).toHaveClass(BUTTON_PRESSED_CLASSES.danger);
    expect(cancel).toHaveClass('cursor-pointer');
    expect(confirm).toHaveClass('cursor-pointer');
  });

  it('never paints a pressed state on a confirmation that cannot be pressed', () => {
    renderOpenDialog({ pending: true });

    const confirm = screen.getByRole('button', { name: 'Delete profile' });
    expect(confirm).toBeDisabled();
    expect(confirm).not.toHaveClass(BUTTON_PRESSED_CLASSES.danger);
    // Not one `active:` utility survives on the locked control.
    expect(Array.from(confirm.classList).some((name) => name.includes('active:'))).toBe(false);

    // The way out keeps both its press feedback and its focus ring.
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(cancel).toBeEnabled();
    expect(cancel).toHaveClass(BUTTON_PRESSED_CLASSES.ghost);
    expect(cancel).toHaveClass('focus-visible:ring-2');
  });

  it('keeps the keyboard focus cycling through the controls of the dialog only', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const trigger = screen.getByRole('button', { name: 'Open delete dialog' });
    await user.click(trigger);

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const confirm = screen.getByRole('button', { name: 'Delete profile' });
    expect(cancel).toHaveFocus();

    // Shift+Tab on the first control wraps to the last one of the dialog...
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    expect(trigger).not.toHaveFocus();

    // ...and Tab on the last one wraps back to the first: focus never escapes.
    await user.tab();
    expect(cancel).toHaveFocus();
    expect(trigger).not.toHaveFocus();
  });

  it('keeps the confirmation dialog on the named md container step (28rem, 448px)', () => {
    renderOpenDialog();

    // `max-w-md` compiles to 28rem = 448px through the width-scale bridge of
    // globals.css. Measured live before that bridge existed: this dialog was a
    // 50 x 744 px sliver, because the name resolved to the 8px `--spacing-md`
    // density token instead of the container step.
    expect(screen.getByRole('dialog')).toHaveClass('max-w-md');
  });
});
