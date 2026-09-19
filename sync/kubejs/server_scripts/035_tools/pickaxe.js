// server_scripts/030_tools/pickaxe.js

ServerEvents.tags('block', event => {
  console.info("=== 030_tools/pickaxe Tool Tags script loaded ===");

  // ============================================================
  // GLOWSTONE
  // Glowstone → Pickaxe required, minimum iron tier
  // ============================================================
  event.add('minecraft:mineable/pickaxe', 'minecraft:glowstone');
  event.add('minecraft:needs_iron_tool', 'minecraft:glowstone');
});
