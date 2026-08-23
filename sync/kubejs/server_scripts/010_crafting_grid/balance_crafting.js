ServerEvents.recipes(event => {
  console.info("=== 010_crafting_grid/balance_crafting Recipe script loaded ===");

  // Craftable Saddle
  event.shaped(
    'minecraft:saddle',
    [
      'LLL',
      'LIL',
      'THT'
    ],
    {
      L: 'minecraft:leather',
      I: 'minecraft:iron_ingot',
      T: 'minecraft:tripwire_hook',
      H: 'minecraft:string'
    }
  ).id('kubejs:craftable_saddle');

  // Wooden Spool (replaces old spool) – output 16 spools
  event.shaped(
    Item.of('createaddition:spool', 16),
    [
      'O',
      'S',
      'O'
    ],
    {
      O: 'minecraft:oak_slab',
      S: 'minecraft:stick'
    }
  ).id('kubejs:wooden_spool');

  // Wooden Sail Frame
  event.shaped(
    'create:sail_frame',
    [
      'SSS',
      'S S',
      'SSS'
    ],
    {
      S: 'minecraft:stick'
    }
  ).id('kubejs:wooden_sail_frame');

});