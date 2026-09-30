// Text of the production design document, in four parts (sections 1–7, 8–13, 14–21, 22–27).
import { foundations } from "./production/01_foundations.mjs";
import { integrationsA } from "./production/02_integrations_a.mjs";
import { integrationsB } from "./production/03_integrations_b.mjs";
import { platform } from "./production/04_platform.mjs";

export function content(kit, opts) {
  foundations(kit, opts);
  integrationsA(kit, opts);
  integrationsB(kit, opts);
  platform(kit, opts);
}
