ServerEvents.recipes(event => {
  console.info("=== 011_machines/cutting Recipe script loaded ===");

  // Melon → 9 slices + 2 seeds guaranteed + 1 extra seed (25% chance)
  event.recipes.create.cutting(
    [
      Item.of('minecraft:melon_slice', 9),
      Item.of('minecraft:melon_seeds', 2),
      Item.of('minecraft:melon_seeds').withChance(0.25)
    ],
    'minecraft:melon',
    50
  ).id('kubejs:melon_slices'); // optional custom ID, matches the CT recipe name


});