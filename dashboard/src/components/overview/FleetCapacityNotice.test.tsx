import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { FleetCapacityNotice, fleetCapacitySentence, positiveOrNull } from "./FleetCapacityNotice";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

/** Props of a healthy read: a cap, a slot pair and one waiting profile. */
const KNOWN = {
  maxRunningProfiles: 10,
  engineSlotsUsed: 10,
  engineSlotsTotal: 10,
  waitingProfiles: 1,
};

describe("positiveOrNull", () => {
  it("keeps a usable count", () => {
    expect(positiveOrNull(10)).toBe(10);
  });

  it("answers null for an absent, zero or non-finite value", () => {
    expect(positiveOrNull(undefined)).toBeNull();
    expect(positiveOrNull(Number.NaN)).toBeNull();
    expect(positiveOrNull(Number.POSITIVE_INFINITY)).toBeNull();
    expect(positiveOrNull(0)).toBeNull();
  });
});

describe("fleetCapacitySentence", () => {
  it("derives the whole sentence from the settings and health it is given", () => {
    expect(fleetCapacitySentence({ ...KNOWN, staggerSeconds: 12 })).toBe(
      "The engine runs at most 10 profiles at a time (10 of 10 slots in use). " +
        "The limit is deliberate: one Freqtrade worker costs about 390 MiB of memory. " +
        "A queued profile is not an error - it starts automatically as soon as a slot frees. " +
        "Workers start one at a time, 12 seconds apart, so a cold start never overloads the host.",
    );
  });

  it("falls back to the slot total of the health payload when no cap is published", () => {
    const sentence = fleetCapacitySentence({
      engineSlotsUsed: 3,
      engineSlotsTotal: 8,
      waitingProfiles: 0,
    });

    expect(sentence).toContain("The engine runs at most 8 profiles at a time (3 of 8 slots in use).");
    expect(sentence).not.toContain("NaN");
    expect(sentence).not.toContain("undefined");
  });

  it("leaves the used slot count out when the health read published none", () => {
    const sentence = fleetCapacitySentence({
      maxRunningProfiles: 4,
      engineSlotsUsed: Number.NaN,
      engineSlotsTotal: Number.NaN,
      waitingProfiles: 2,
    });

    expect(sentence).toContain("The engine runs at most 4 profiles at a time.");
    expect(sentence).not.toContain("slots in use");
    expect(sentence).not.toContain("NaN");
  });

  it("states honestly that the cap could not be read when both reads are unknown", () => {
    const sentence = fleetCapacitySentence({
      engineSlotsUsed: Number.NaN,
      engineSlotsTotal: Number.NaN,
      waitingProfiles: 3,
    });

    expect(sentence).toBe(
      "The engine runs a limited number of profiles at a time (the current cap could not be read). " +
        "The limit is deliberate: one Freqtrade worker costs about 390 MiB of memory. " +
        "A queued profile is not an error - it starts automatically as soon as a slot frees.",
    );
    expect(sentence).not.toContain("NaN");
    expect(sentence).not.toContain("undefined");
  });

  it("never prints NaN, whatever the payload carries", () => {
    const sentence = fleetCapacitySentence({
      maxRunningProfiles: Number.NaN,
      engineSlotsUsed: Number.POSITIVE_INFINITY,
      engineSlotsTotal: Number.NaN,
      waitingProfiles: 0,
      staggerSeconds: Number.NaN,
    });

    expect(sentence).not.toMatch(/NaN/);
    expect(sentence).not.toMatch(/undefined/);
    expect(sentence).not.toMatch(/Infinity/);
  });

  it("omits the stagger clause when the supervisor publishes a zero delay", () => {
    const withZero = fleetCapacitySentence({ ...KNOWN, staggerSeconds: 0 });
    const without = fleetCapacitySentence({ ...KNOWN });

    expect(withZero).toBe(without);
    expect(withZero).not.toContain("Workers start one at a time");
  });

  it("keeps the same sentence when profiles wait", () => {
    expect(fleetCapacitySentence({ ...KNOWN, waitingProfiles: 0 })).toBe(
      fleetCapacitySentence({ ...KNOWN, waitingProfiles: 7 }),
    );
  });

  it("never calls a queued profile a problem", () => {
    const sentence = fleetCapacitySentence({ ...KNOWN, waitingProfiles: 4 });

    expect(sentence).toContain("A queued profile is not an error");
    expect(sentence).not.toMatch(/needing attention/i);
  });
});

describe("FleetCapacityNotice", () => {
  it("renders the sentence as always-visible text", () => {
    render(<FleetCapacityNotice {...KNOWN} staggerSeconds={12} />);

    const notice = screen.getByRole("note");
    expect(notice).toHaveTextContent(
      "The engine runs at most 10 profiles at a time (10 of 10 slots in use).",
    );
    expect(notice).toHaveTextContent("Workers start one at a time, 12 seconds apart");
    expect(notice).toHaveTextContent("1 profile is waiting for a slot in this section.");
    // Ordinary text, never a tooltip and never a hover-only affordance.
    expect(notice).not.toHaveAttribute("title");
  });

  it("counts the waiting profiles of the section in the plural", () => {
    render(<FleetCapacityNotice {...KNOWN} waitingProfiles={3} />);

    expect(
      screen.getByText("3 profiles are waiting for a slot in this section."),
    ).toBeInTheDocument();
  });

  it("says so when no profile of the section waits", () => {
    render(<FleetCapacityNotice {...KNOWN} waitingProfiles={0} />);

    expect(screen.getByText("No profile of this section is waiting for a slot.")).toBeInTheDocument();
  });

  it("still renders the sentence when the settings and the health are unknown", () => {
    render(
      <FleetCapacityNotice
        engineSlotsUsed={Number.NaN}
        engineSlotsTotal={Number.NaN}
        waitingProfiles={2}
      />,
    );

    const notice = screen.getByRole("note");
    expect(notice).toHaveTextContent("the current cap could not be read");
    expect(screen.queryByText(/NaN/)).toBeNull();
    expect(screen.queryByText(/undefined/)).toBeNull();
  });
});
