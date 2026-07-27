ServerEvents.recipes(event => {

    // Remove vanilla stonecutter
    event.remove({type: 'minecraft:stonecutting',output: 'create:rose_quartz_block'})
    

    // Remove Create cutting
    event.remove({type: 'create:cutting',output: 'create:rose_quartz_block'})

    // Add 1:1 cutting
    event.stonecutting('create:rose_quartz_block','create:rose_quartz')
    
    // Add 1:1 crushing wheels
    event.recipes.create.crushing(['create:rose_quartz'],'create:rose_quartz_block')
    event.recipes.create.crushing(['create:rose_quartz'],'biomesoplenty:rose_quartz_block')

})