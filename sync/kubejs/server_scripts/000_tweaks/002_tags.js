const STRIPPED_REGEX = /:stripped_.*_(log|wood)$/;

ServerEvents.tags('item', event => {
  console.info("=== 002_tags Recipe script loaded ===");

  // ----- Existing: automatically add all stripped logs to forge:stripped_logs -----
  const existing = new Set(
    event.get('forge:stripped_logs')
         .getObjectIds()
         .toArray()
  );

  const discovered = Ingredient.of(STRIPPED_REGEX)
                               .getItemIds()
                               .toArray();

  discovered.forEach(id => {
    if (!existing.has(id)) {
      event.add('forge:stripped_logs', id);
    }
  });

  // ----- Nickel Unification (TFMG → Immersive Engineering) -----
  // Remove TFMG nickel items from the main forge tags
  event.remove('forge:ingots/nickel', 'tfmg:nickel_ingot');
  event.remove('forge:dusts/nickel', 'tfmg:nickel_dust');
  event.remove('forge:ores/nickel', 'tfmg:nickel_ore');
  event.remove('forge:raw_materials/nickel', 'tfmg:raw_nickel');

  // Add IE nickel items to those tags (unified)
  event.add('forge:ingots/nickel', 'immersiveengineering:ingot_nickel');
  event.add('forge:dusts/nickel', 'immersiveengineering:dust_nickel');
  event.add('forge:ores/nickel', 'immersiveengineering:ore_nickel');
  event.add('forge:raw_materials/nickel', 'immersiveengineering:raw_nickel');

});

ServerEvents.tags('block', event => {

  // ----- Existing: auto‑add stripped logs as blocks (for block tags) -----
  const existing = new Set(
    event.get('forge:stripped_logs')
         .getObjectIds()
         .toArray()
  );

  const discovered = Ingredient.of(STRIPPED_REGEX)
                               .getItemIds()
                               .toArray();

  discovered.forEach(id => {
    event.add('forge:stripped_logs', id);
  });

});