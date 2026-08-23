ServerEvents.recipes(event => {
    console.info("=== 011_machines/crushing Recipe script loaded ===");

    // Add 1:1 cutting
    event.stonecutting('create:rose_quartz_block','create:rose_quartz')
    
    // Add 1:1 crushing wheels
    event.recipes.create.crushing(['create:rose_quartz'],'create:rose_quartz_block')
    event.recipes.create.crushing(['create:rose_quartz'],'biomesoplenty:rose_quartz_block')

})