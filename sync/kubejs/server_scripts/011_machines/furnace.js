ServerEvents.recipes(event => {
  // ----- Rotten Flesh → Leather (furnace) -----
  event.smelting('minecraft:leather', 'minecraft:rotten_flesh')
    .xp(1.5)
    .cookingTime(100)
    .id('kubejs:fleshtoleather');

  // ----- Raw block conversions (furnace) -----
  const rawBlocks = [
    ['minecraft:raw_copper_block', 'minecraft:copper_block'],
    ['minecraft:raw_iron_block',   'minecraft:iron_block'],
    ['minecraft:raw_gold_block',   'minecraft:gold_block']
  ];

  rawBlocks.forEach(([input, output], index) => {
    event.smelting(output, input)
      .xp(1.0)
      .cookingTime(200)
      .id(`kubejs:raw_block_conversion_${index}`);
  });

});