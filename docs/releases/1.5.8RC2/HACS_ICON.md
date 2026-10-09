# HACS icon investigation — 9 October 2026

The integration ships local branding in
`custom_components/hoymiles_hit_modbus/brand/`: `icon.png`, `icon@2x.png`,
`dark_icon.png` and `dark_icon@2x.png`. These files belong in the distributed
integration and are covered by the source-archive checks.
The icons are 256×256 and 512×512 pixels; light/dark logos (800×240) are
also included. All six PNG headers and dimensions were verified locally.

[Home Assistant's local brands API](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
supports this layout from HA 2026.3. The project's earlier
[brands submission #10932](https://github.com/home-assistant/brands/pull/10932)
was closed **without merging** on 8 August 2026. The
[maintainer explanation](https://github.com/home-assistant/brands/pull/10932#issuecomment-5226116437)
directed custom integrations to local branding. Closure was not evidence that
these images failed a quality check.

## Published HACS frontend limitation

At inspection time, the latest published HACS integration was
[2.0.5](https://github.com/hacs/integration/releases/tag/2.0.5), with frontend
[20250128065759](https://github.com/hacs/frontend/releases/tag/20250128065759).
Its [dashboard source](https://github.com/hacs/frontend/blob/20250128065759/src/dashboards/hacs-dashboard.ts)
uses the brands URL helper from its pinned Home Assistant frontend submodule
`3ffbd435e0e5cf23872057187f3da53bb62441a2`.
That [helper](https://github.com/home-assistant/frontend/blob/3ffbd435e0e5cf23872057187f3da53bb62441a2/src/util/brands-url.ts)
constructs URLs on `brands.home-assistant.io`, rather than requesting local custom
integration branding through the authenticated HA API. The icon was not merged
into that old CDN, so shipping local images alone cannot fix that HACS display path.

Matching upstream reports [#5223](https://github.com/hacs/integration/issues/5223)
and [#5171](https://github.com/hacs/integration/issues/5171) were still open.
This is a supported upstream explanation, not proof of the HACS build installed
on any particular host: installed HACS versions and browser network requests were
not read during this publication preparation.

Keep the local brand files. Do not create another obsolete brands submission,
modify installed HACS, or add a proxy/authentication workaround for the icon.
Publishing this EMS version does not by itself repair the older HACS frontend.
Recheck an upstream HACS fix before claiming that its icon support is resolved.

The canonical repository name is awaiting the existing
[HACS default-list rename #10003](https://github.com/hacs/default/pull/10003).
The inspected list already contains its previous name,
`Kaluzaburza/Hoymiles_HIT_xxL_G3_ModBus`, which redirects to this repository.
Do not submit a duplicate inclusion request. Updating that registry name and
publishing a GitHub release are separate steps; the rename does not repair
the legacy CDN lookup for a repository already available to the user.

## Rechecked 9 October 2026

Latest published HACS remains **2.0.5**. The relevant fixes are still open:
[frontend #937](https://github.com/hacs/frontend/pull/937),
[integration #5388](https://github.com/hacs/integration/pull/5388) and
[frontend #949](https://github.com/hacs/frontend/pull/949). None was merged at
this check. Icons visible for some other integrations can still come from the
older shared CDN; that is not evidence that local custom icons work in 2.0.5.

The supported mitigation in this release is a version-pinned README logo
(`render_readme: true`) plus the six local HA brand images. This gives the
repository details their own branding; it does **not** repair the old HACS
repository-list lookup. No domain rename, HACS runtime patch, fake core domain
or unsupported `hacs.json` icon field is introduced.
