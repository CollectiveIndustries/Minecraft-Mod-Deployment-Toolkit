// server_scripts/crushing.js
ServerEvents.recipes(event => {
  console.info("=== 011_machines/crushing Recipe script loaded ===");

  // Cobbled Deepslate → Gravel + Experience Nugget (1% chance)
  event.recipes.create.crushing(
    [
      'minecraft:gravel',                                    // 100% (always)
      'create_more_features:pieces_of_graphite',            // 100% (guaranteed)
      Item.of('create_more_features:pieces_of_graphite').withChance(0.4), // 40% bonus
      Item.of('create:experience_nugget').withChance(0.01)  // 1% chance
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

  // ============================================================
  // NEW: Slag Gravel → Sand + trace metals + specialty minerals
  // Based on realistic blast furnace slag composition
  // ============================================================
  event.recipes.create.crushing(
    [
      // PRIMARY: The bulk silicate material
      '4x minecraft:sand',

      // IRON FAMILY: The most common metal oxide in slag (0.5% - 2% of total mass)
      Item.of('minecraft:iron_nugget').withChance(0.35), // 35%

      // BASE METAL TRACES: These partition into slag as impurities (< 1% each)
      Item.of('create:copper_nugget').withChance(0.10), // 10%
      Item.of('create:zinc_nugget').withChance(0.08),   // 8%
      Item.of('create:nickel_nugget').withChance(0.03), // 3% (rare trace)

      // YOUR REQUESTED SPECIALTY OUTPUTS:
      // Magnetite (Fe₃O₄) - a primary crystalline phase in cooled slag
      Item.of('create_new_age:magnetite_block').withChance(0.15), // 15%
      
      // Silicates (Dicalcium/Tricalcium) - represented by AE2 quartz dust
      Item.of('ae2:certus_quartz_dust').withChance(0.10)          // 10%
    ],
    'immersiveengineering:slag_gravel',
    200   // same processing time as your other recipes
  );
});