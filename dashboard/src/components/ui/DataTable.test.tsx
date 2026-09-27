import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DataTable, type DataTableColumn } from "./DataTable";

interface Row {
  id: string;
  name: string;
  value: number;
}

const ROWS: Row[] = [
  { id: "b", name: "Bravo", value: 20 },
  { id: "a", name: "Alpha", value: 30 },
  { id: "c", name: "Charlie", value: 20 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  { id: "name", header: "Name", render: (row) => row.name, sortValue: (row) => row.name },
  {
    id: "value",
    header: "Value",
    align: "end",
    render: (row) => String(row.value),
    sortValue: (row) => row.value,
  },
  { id: "mode", header: "Mode", render: () => "paper" },
];

function renderTable(overrides: Partial<ComponentProps<typeof DataTable<Row>>> = {}) {
  return render(
    <DataTable
      columns={COLUMNS}
      rows={ROWS}
      rowKey={(row) => row.id}
      caption="Profiles"
      emptyMessage="Nothing here."
      {...overrides}
    />,
  );
}

/** Body rows of the rendered table. */
function bodyRows(): string[] {
  const groups = screen.getAllByRole("rowgroup");
  return within(groups[1])
    .getAllByRole("row")
    .map((row) => row.textContent ?? "");
}

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("DataTable", () => {
  it("keeps the incoming row order and names the table for screen readers", () => {
    renderTable();

    expect(screen.getByRole("table", { name: "Profiles" })).toBeInTheDocument();
    expect(bodyRows()).toEqual(["Bravo20paper", "Alpha30paper", "Charlie20paper"]);
  });

  it("renders every sortable header as a real button and the others as text", () => {
    renderTable();

    expect(screen.getByRole("button", { name: "Sort by Name" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sort by Value" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sort by Mode" })).not.toBeInTheDocument();
  });

  it("exposes aria-sort on the sortable headers only", () => {
    renderTable();

    const nameHeader = screen.getByRole("columnheader", { name: /Name/ });
    const modeHeader = screen.getByRole("columnheader", { name: "Mode" });

    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    expect(modeHeader).not.toHaveAttribute("aria-sort");
  });

  it("cycles a header through ascending, descending and back to none", () => {
    renderTable();
    const button = screen.getByRole("button", { name: "Sort by Value" });
    const header = screen.getByRole("columnheader", { name: /Value/ });

    fireEvent.click(button);
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(bodyRows()).toEqual(["Bravo20paper", "Charlie20paper", "Alpha30paper"]);

    fireEvent.click(button);
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(bodyRows()).toEqual(["Alpha30paper", "Bravo20paper", "Charlie20paper"]);

    fireEvent.click(button);
    expect(header).toHaveAttribute("aria-sort", "none");
    expect(bodyRows()).toEqual(["Bravo20paper", "Alpha30paper", "Charlie20paper"]);
  });

  it("sorts strings and numbers with their own comparator", () => {
    renderTable();

    fireEvent.click(screen.getByRole("button", { name: "Sort by Name" }));
    expect(bodyRows()[0]).toBe("Alpha30paper");

    fireEvent.click(screen.getByRole("button", { name: "Sort by Value" }));
    expect(bodyRows()[0]).toBe("Bravo20paper");
  });

  it("applies the row class of the caller", () => {
    renderTable({ rowClassName: (row) => (row.value === 30 ? "attention" : undefined) });

    const groups = screen.getAllByRole("rowgroup");
    const rows = within(groups[1]).getAllByRole("row");
    expect(rows[1]).toHaveClass("attention");
    expect(rows[0]).not.toHaveClass("attention");
  });

  it("renders the empty message inside the table when there is no row", () => {
    renderTable({ rows: [] });

    expect(screen.getAllByRole("row")).toHaveLength(2); // header row + empty row
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
  });

  it("scrolls inside its own container instead of the page", () => {
    const { container } = renderTable();

    expect(container.querySelector("div.overflow-x-auto")).not.toBeNull();
  });

  it("keeps the row key stable across a re-render", () => {
    const rowKey = vi.fn((row: Row) => row.id);
    const { rerender } = renderTable({ rowKey });

    rerender(
      <DataTable
        columns={COLUMNS}
        rows={ROWS}
        rowKey={rowKey}
        caption="Profiles"
        emptyMessage="Nothing here."
      />,
    );

    expect(bodyRows()).toHaveLength(3);
    expect(rowKey).toHaveBeenCalled();
  });
});
