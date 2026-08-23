ServerEvents.recipes(event => {
  console.info("=== 000_removal Recipe script loaded ===");

  // ----- Remove all recipes that output TFMG nickel items -----
  event.remove({ output: 'tfmg:nickel_ingot' });
  event.remove({ output: 'tfmg:nickel_dust' });
  event.remove({ output: 'tfmg:nickel_ore' });
  event.remove({ output: 'tfmg:raw_nickel' });

  // Remove crafting table chain (original)
  event.remove({ output: 'minecraft:chain' });

  // Remove vanilla stonecutter
  event.remove({type: 'minecraft:stonecutting',output: 'create:rose_quartz_block'})

  // Remove Create cutting
  event.remove({type: 'create:cutting',output: 'create:rose_quartz_block'})

  // Remove all Create splashing recipes that use sand as input
  event.remove({ type: 'create:splashing', input: 'minecraft:sand' });

  // Remove the sequenced assembly recipe that outputs Create tracks
  event.remove({ type: 'create:sequenced_assembly', output: 'create:track' });

  // Remove all vanilla crafting table recipes that output createaddition:spool
  event.remove({ output: 'createaddition:spool' });

  // ----- Remove Smooth Stone from furnace (smelting) -----
  event.remove({ type: 'minecraft:smelting', output: 'minecraft:smooth_stone' });

  // ----- Remove Smooth Stone from stonecutter (we'll use deployer instead) -----
  event.remove({ type: 'minecraft:stonecutting', output: 'minecraft:smooth_stone' });

  // remove the Minecraft LEAD recipe (we'll use a rod-based recipe instead)
  event.remove({ output: 'minecraft:lead' })

  // Remove the old recipe that gave graphite from cobbled deepslate
  event.remove({ id: 'create_more_features:piecesofgraphiterecipe' });

});
