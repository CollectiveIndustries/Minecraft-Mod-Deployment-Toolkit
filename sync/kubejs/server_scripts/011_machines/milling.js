ServerEvents.recipes(event => {
  console.info("=== 011_machines/milling Recipe script loaded ===");

  // Andesite → Andesite Alloy (25%), Gravel (5%), Cobblestone (25%)
  event.recipes.create.milling(
    [
      Item.of('create:andesite_alloy').withChance(0.25),
      Item.of('minecraft:gravel').withChance(0.05),
      Item.of('minecraft:cobblestone').withChance(0.25)
    ],
    'minecraft:andesite',
    200
  ).id('kubejs:create_milled_andesite_alloy');

});