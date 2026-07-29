using System;
using System.Diagnostics;
using System.IO;
using System.Linq;

namespace CodeMap.Extractors
{
    public static class WindowsWorker
    {
        public static int Run(string[] args)
        {
            if (!OperatingSystem.IsWindows())
            {
                Console.Error.WriteLine("WindowsWorker can only run on Windows.");
                return 1;
            }

            var configPath = args.FirstOrDefault(a => a.StartsWith("--config="))?.Split('=')[1] 
                             ?? args.SkipWhile(a => a != "--config").Skip(1).FirstOrDefault();

            if (string.IsNullOrEmpty(configPath))
            {
                Console.Error.WriteLine("Missing --config argument.");
                return 1;
            }

            Console.WriteLine($"[WindowsWorker] Running MSBuild/Roslyn extraction for config: {configPath}");
            
            // In a real implementation, this would invoke the MSBuildWorkspace logic
            // For scaffolding, we just return success to indicate the worker was called
            return 0;
        }
    }
}
