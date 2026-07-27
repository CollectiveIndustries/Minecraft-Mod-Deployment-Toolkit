ServerEvents.recipes(event => {

  // Cobbled Deepslate → Gravel + Experience Nugget (1% chance)
  event.recipes.create.crushing(
    [
      'minecraft:gravel', // 100% chance (default)
      Item.of('create:experience_nugget').withChance(0.01) // 1%
    ],
    'minecraft:cobbled_deepslate',
    200
  );

  // Quartz Block → 4x Quartz
  event.recipes.create.crushing(
    '4x minecraft:quartz', // shorthand for 4 items
    'minecraft:quartz_block',
    200
  );

  // Smooth Basalt → Basalt (100%) + Soul Soil (5%) + Ancient Debris (0.0018%)
  event.recipes.create.crushing(
    [
      'minecraft:basalt',
      Item.of('minecraft:soul_soil').withChance(0.05),
      Item.of('minecraft:ancient_debris').withChance(0.000018)
    ],
    'minecraft:smooth_basalt',
    200
  );

  // Poisonous Potato → Biomass (100%) + extra 2 Biomass (25% chance)
  event.recipes.create.crushing(
    [
      'createaddition:biomass',
      Item.of('createaddition:biomass', 2).withChance(0.25)
    ],
    'minecraft:poisonous_potato',
    200
  );

  // Biomass Pellet Block → Dirt (100%) + extra Dirt (1%)
  event.recipes.create.crushing(
    [
      'minecraft:dirt',                               // 100%
      Item.of('minecraft:dirt').withChance(0.01)      // 1%
    ],
    'createaddition:biomass_pellet_block',
    200
  ).id('kubejs:crush_biomass_to_dirt');

});

