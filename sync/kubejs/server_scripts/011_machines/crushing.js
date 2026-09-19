// server_scripts/crushing.js

ServerEvents.recipes(event => {
  console.info("=== 011_machines/crushing Recipe script loaded ===");


  // ============================================================
  // COBBLED DEEPSLATE
  // Cobbled Deepslate → Gravel + Graphite + Experience Nugget
  // ============================================================
  event.recipes.create.crushing(
    [
      'minecraft:gravel',                                                // 100%
      'create_more_features:pieces_of_graphite',                        // 100%
      Item.of('create_more_features:pieces_of_graphite').withChance(0.4), // 40%
      Item.of('create:experience_nugget').withChance(0.01)               // 1%
    ],
    'minecraft:cobbled_deepslate',
    200
  );


  // ============================================================
  // QUARTZ BLOCK
  // Quartz Block → 4x Quartz
  // ============================================================
  event.recipes.create.crushing(
    '4x minecraft:quartz',
    'minecraft:quartz_block',
    200
  );


  // ============================================================
  // SMOOTH BASALT
  // Smooth Basalt → Basalt + Soul Soil + Ancient Debris
  // ============================================================
  event.recipes.create.crushing(
    [
      'minecraft:basalt',                                          // 100%
      Item.of('minecraft:soul_soil').withChance(0.05),            // 5%
      Item.of('minecraft:ancient_debris').withChance(0.000018)    // 0.0018%
    ],
    'minecraft:smooth_basalt',
    200
  );


  // ============================================================
  // POISONOUS POTATO
  // Poisonous Potato → Biomass + bonus Biomass
  // ============================================================
  event.recipes.create.crushing(
    [
      'createaddition:biomass',                                   // 100%
      Item.of('createaddition:biomass', 2).withChance(0.25)       // 25%
    ],
    'minecraft:poisonous_potato',
    200
  );


  // ============================================================
  // BIOMASS PELLET BLOCK
  // Biomass Pellet Block → Dirt + bonus Dirt
  // ============================================================
  event.recipes.create.crushing(
    [
      'minecraft:dirt',                                          // 100%
      Item.of('minecraft:dirt').withChance(0.01)                 // 1%
    ],
    'createaddition:biomass_pellet_block',
    200
  ).id('kubejs:crush_biomass_to_dirt');


  // ============================================================
  // SLAG GRAVEL
  // Slag Gravel → Sand + trace metals + specialty minerals
  // ============================================================
  event.recipes.create.crushing(
    [
      // PRIMARY: Bulk silicate material
      '4x minecraft:sand',

      // IRON FAMILY
      Item.of('minecraft:iron_nugget').withChance(0.35),

      // BASE METAL TRACES
      Item.of('create:copper_nugget').withChance(0.10),
      Item.of('create:zinc_nugget').withChance(0.08),
      Item.of('create:nickel_nugget').withChance(0.03),

      // SPECIALTY OUTPUTS
      Item.of('create_new_age:magnetite_block').withChance(0.15),
      Item.of('ae2:certus_quartz_dust').withChance(0.10)
    ],
    'immersiveengineering:slag_gravel',
    200
  );


    // ============================================================
  // LIMESTONE
  //
  // create:limestone
  //
  // Guaranteed:
  //   1x tfmg:limesand
  //   1x create_more_features:saltpeter
  //
  // Bonus:
  //   1x saltpeter 50%
  //   1x saltpeter 20%
  //
  // Nuclear:
  //   1x nitrate 60%
  //   1x lead nugget 40%
  //
  // Additional:
  //   1x quartz 12%
  //   1x lapis lazuli 8%
  // ============================================================

  event.custom({
    type: 'create:crushing',

    ingredients: [
      {
        item: 'create:limestone'
      }
    ],

    results: [
      {
        item: 'tfmg:limesand',
        count: 1,
        chance: 1.0
      },

      {
        item: 'create_more_features:saltpeter',
        count: 1,
        chance: 1.0
      },

      {
        item: 'create_more_features:saltpeter',
        count: 1,
        chance: 0.50
      },

      {
        item: 'create_more_features:saltpeter',
        count: 1,
        chance: 0.20
      },

      {
        item: 'createnuclear:nitrate',
        count: 1,
        chance: 0.60
      },

      {
        item: 'createnuclear:lead_nugget',
        count: 1,
        chance: 0.40
      },

      {
        item: 'minecraft:quartz',
        count: 1,
        chance: 0.12
      },

      {
        item: 'minecraft:lapis_lazuli',
        count: 1,
        chance: 0.08
      }
    ],

    processingTime: 200
  });
  
  // Coal → Unified Coal Dust + Coal Pins
event.recipes.create.crushing(
  [
    'create_ironworks:coal_dust',                                      // 100%
    Item.of('createnuclear:coal_dust').withChance(0.50),              // 50%
    Item.of('create_more_features:coal_pin').withChance(0.30),       // 30%
    Item.of('create_more_features:coal_pin').withChance(0.90)        // 90%
  ],
  'minecraft:coal',
  200
).id('kubejs:coal_unified_crushing');

console.info(
  "[ADD] Unified coal crushing: minecraft:coal -> " +
  "create_ironworks:coal_dust + " +
  "createnuclear:coal_dust (50%) + " +
  "create_more_features:coal_pin (30%) + " +
  "create_more_features:coal_pin (90%)"
);

  // ============================================================
  // RADIOACTIVE THORIUM
  // Create New Age Radioactive Thorium → Create Nuclear Thorium Dust
  // ============================================================
  event.recipes.create.crushing(
    'createnuclear:thorium_dust',
    'create_new_age:radioactive_thorium',
    200
  ).id('kubejs:thorium_dust_conversion');


  // ============================================================
  // MAGNETITE BLOCK
  // Create New Age Magnetite Block → Crushed Raw Iron
  // ============================================================
  event.recipes.create.crushing(
    '2x create:crushed_raw_iron',
    'create_new_age:magnetite_block',
    200
  ).id('kubejs:magnetite_to_crushed_iron');

});
