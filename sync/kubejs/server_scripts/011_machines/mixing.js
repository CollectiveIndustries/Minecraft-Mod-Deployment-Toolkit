ServerEvents.recipes(event => {
  console.info("=== 011_machines/mixing Recipe script loaded ===");

  // Red Alloy (heated) – Copper Ingot + 4 Redstone → Red Alloy Ingot
  event.recipes.create.mixing(
    'morered:red_alloy_ingot',                // output (1)
    [ 'minecraft:copper_ingot', '4x minecraft:redstone' ] // inputs
  )
  .heated()                                   // heat condition: heated
  .processingTime(200)
  .id('kubejs:red_alloy');

  // Crying Obsidian (super-heated) – Obsidian + Diamond Grit + 500mB Lava
  // Output: Crying Obsidian (100%) + 2 Ghast Tears (0.001% chance)
  event.recipes.create.mixing(
    [
      'minecraft:crying_obsidian',
      Item.of('minecraft:ghast_tear', 2).withChance(0.00001) // 0.001% = 0.00001
    ],
    [
      'minecraft:obsidian',
      'createaddition:diamond_grit',
      Fluid.of('minecraft:lava', 500)        // fluid input
    ]
  )
  .superheated()                              // heat condition: superheated
  .processingTime(1200)
  .id('kubejs:mixed_crying_obsidian');

// Sawdust + Water → Paper
// 4x Immersive Engineering sawdust + 250mB water = 1 paper

event.recipes.create.mixing(
  'minecraft:paper',
  [
    '4x immersiveengineering:dust_wood',
    Fluid.of('minecraft:water', 250)
  ]
)
.processingTime(100)
.id('kubejs:paper_from_sawdust');

});
