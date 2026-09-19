ServerEvents.recipes(event => {
  console.info("==================================================");
  console.info("=== 000_removal Recipe script loaded ===");
  console.info("==================================================");

  // ----- TFMG Nickel -----
  console.info("[REMOVE] All recipes outputting tfmg:nickel_ingot");
  event.remove({ output: 'tfmg:nickel_ingot' });

  console.info("[REMOVE] All recipes outputting tfmg:nickel_dust");
  event.remove({ output: 'tfmg:nickel_dust' });

  console.info("[REMOVE] All recipes outputting tfmg:nickel_ore");
  event.remove({ output: 'tfmg:nickel_ore' });

  console.info("[REMOVE] All recipes outputting tfmg:raw_nickel");
  event.remove({ output: 'tfmg:raw_nickel' });
  
// ----- Iron Trapdoor -----
// Remove vanilla iron trapdoor recipe.
// This also prevents Create Automated Packing from using the recipe.
console.info("[REMOVE] Recipe: minecraft:iron_trapdoor");

event.remove({
  id: 'minecraft:iron_trapdoor'
});


  // ----- Chain -----
  console.info("[REMOVE] Vanilla crafting recipes outputting minecraft:chain");
  event.remove({ output: 'minecraft:chain' });


  // ----- Rose Quartz Block -----
  console.info("[REMOVE] Vanilla stonecutting -> create:rose_quartz_block");
  event.remove({
    type: 'minecraft:stonecutting',
    output: 'create:rose_quartz_block'
  });

  console.info("[REMOVE] Create cutting -> create:rose_quartz_block");
  event.remove({
    type: 'create:cutting',
    output: 'create:rose_quartz_block'
  });


  // ----- Create Splashing -----
  console.info("[REMOVE] Create splashing recipes using minecraft:sand");
  event.remove({
    type: 'create:splashing',
    input: 'minecraft:sand'
  });


  // ----- Create Tracks -----
  console.info("[REMOVE] Create sequenced assembly recipes outputting create:track");
  event.remove({
    type: 'create:sequenced_assembly',
    output: 'create:track'
  });


  // ----- Create Addition Spool -----
  console.info("[REMOVE] All recipes outputting createaddition:spool");
  event.remove({
    output: 'createaddition:spool'
  });


  // ----- Smooth Stone -----
  console.info("[REMOVE] Vanilla furnace smelting -> minecraft:smooth_stone");
  event.remove({
    type: 'minecraft:smelting',
    output: 'minecraft:smooth_stone'
  });

  console.info("[REMOVE] Vanilla stonecutting -> minecraft:smooth_stone");
  event.remove({
    type: 'minecraft:stonecutting',
    output: 'minecraft:smooth_stone'
  });


  // ----- Lead -----
  console.info("[REMOVE] All recipes outputting minecraft:lead");
  event.remove({
    output: 'minecraft:lead'
  });


  // ----- Graphite -----
  console.info("[REMOVE] Recipe: create_more_features:piecesofgraphiterecipe");
  event.remove({
    id: 'create_more_features:piecesofgraphiterecipe'
  });


  // ----- Industrial Iron Block -----
  console.info(
    "[REMOVE] Recipe: create:industrial_iron_block_from_ingots_iron_stonecutting"
  );

  event.remove({
    id: 'create:industrial_iron_block_from_ingots_iron_stonecutting'
  });

// ============================================================
// LIMESTONE CRUSHING CLEANUP
// ============================================================
console.info("[REMOVE] All Create crushing recipes using create:limestone");

event.remove({
  type: 'create:crushing',
  input: 'create:limestone'
});

// Remove all Create crushing recipes using minecraft:coal
console.info("[REMOVE] All Create crushing recipes using minecraft:coal");
event.remove({
  type: 'create:crushing',
  input: 'minecraft:coal'
});

  // ----- Completion -----
  console.info("==================================================");
  console.info("=== 000_removal Recipe removals registered ===");
  console.info("==================================================");
});
