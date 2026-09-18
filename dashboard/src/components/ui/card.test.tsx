import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Card } from './card';

describe('Card', () => {
  it('renders a heading, a description, actions and its children', () => {
    render(
      <Card title="Equity" description="Last 24 hours" actions={<button type="button">Zoom</button>}>
        <p>Chart</p>
      </Card>,
    );

    expect(screen.getByRole('heading', { level: 2, name: 'Equity' })).toBeInTheDocument();
    expect(screen.getByText('Last 24 hours')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zoom' })).toBeInTheDocument();
    expect(screen.getByText('Chart')).toBeInTheDocument();
  });

  it('renders without a header when nothing is given', () => {
    render(
      <Card>
        <p>Only body</p>
      </Card>,
    );

    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
    expect(screen.getByText('Only body')).toBeInTheDocument();
  });

  it('merges a caller class name', () => {
    const { container } = render(<Card className="col-span-2">body</Card>);
    expect(container.firstElementChild).toHaveClass('col-span-2', 'bg-card', 'border-border');
  });
});
