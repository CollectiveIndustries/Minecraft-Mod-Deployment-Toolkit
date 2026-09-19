ServerEvents.recipes(event => {
  console.info("=== 011_machines/compacting Recipe script loaded ===");

  // Industrial Iron Block (heated)
  // 8 Iron Ingots + 1 Powdered Obsidian → Industrial Iron Block
  event.recipes.create.compacting(
    'create:industrial_iron_block',
    [
      '8x minecraft:iron_ingot',
      'create:powdered_obsidian'
    ]
  )
  .heated()                                  // heated basin
  .processingTime(200)
  .id('kubejs:industrial_iron_block');

  console.info(
    "[ADD] Heated compacting: 8x minecraft:iron_ingot + 1x create:powdered_obsidian -> create:industrial_iron_block"
  );

});
