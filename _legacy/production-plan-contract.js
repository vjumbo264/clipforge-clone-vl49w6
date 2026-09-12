/* ClipForge shared production-plan validation contract loader.
 *
 * The JSON file is the single language-neutral source consumed by this browser
 * and scripts/production_plan_contract.py. It is loaded synchronously only at
 * initial page parsing so app.js never silently falls back to a divergent,
 * hard-coded validator.
 */
(function () {
  'use strict';
  var request = new XMLHttpRequest();
  try {
    request.open('GET', 'schemas/production_plan_contract.json', false);
    request.send(null);
    if (request.status >= 200 && request.status < 300) {
      window.ClipForgeProductionPlanContract = JSON.parse(request.responseText);
      return;
    }
    window.ClipForgeProductionPlanContractError =
      'Could not load schemas/production_plan_contract.json (HTTP ' + request.status + ').';
  } catch (err) {
    window.ClipForgeProductionPlanContractError =
      'Could not load the shared production-plan validation contract: ' + err.message;
  }
}());
